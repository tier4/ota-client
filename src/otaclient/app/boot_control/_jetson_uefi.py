# Copyright 2022 TIER IV, INC. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Boot control implementation for NVIDIA Jetson device boots with UEFI.

jetson-uefi module currently support BSP version >= R35.2.
NOTE: R34~R35.1 is not supported as Capsule update is only available after R35.2.
"""


from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Generator

from otaclient.app import errors as ota_errors
from otaclient.app.configs import config as cfg
from otaclient_api.v2 import types as api_types
from otaclient_common.common import subprocess_call, write_str_to_file_sync, file_sha256
from otaclient_common.typing import StrOrPath

from ._common import CMDHelperFuncs, OTAStatusFilesControl, SlotMountHelper
from ._jetson_common import (
    BSPVersion,
    FirmwareBSPVersionControl,
    NVBootctrlCommon,
    SlotID,
    copy_standby_slot_boot_to_internal_emmc,
    parse_bsp_version,
    preserve_ota_config_files_to_standby,
    update_standby_slot_extlinux_cfg,
)
from .configs import jetson_uefi_cfg as boot_cfg
from .protocol import BootControllerProtocol

logger = logging.getLogger(__name__)

# TODO: calculate this table
L4TLAUNCHER_BSP_VER_SHA256_MAP: dict[str, BSPVersion] = {}


class JetsonUEFIBootControlError(Exception):
    """Exception type for covering jetson-uefi related errors."""


class _NVBootctrl(NVBootctrlCommon):
    """Helper for calling nvbootctrl commands.

    For BSP version >= R34 with UEFI boot.
    Without -t option, the target will be bootloader by default.
    """

    @classmethod
    def get_current_fw_bsp_version(cls) -> BSPVersion:
        """Get current boot chain's firmware BSP version with nvbootctrl."""
        _raw = cls.dump_slots_info()
        """Example:
            Current version: 35.4.1
            Capsule update status: 1
            Current bootloader slot: A
            Active bootloader slot: A
            num_slots: 2
            slot: 0,             status: normal
            slot: 1,             status: normal
        """
        pa = re.compile(r"\s*Current version:\s(?P<bsp_ver>[\.\d]+)\s*")

        if not (ma := pa.search(_raw)):
            _err_msg = "nvbootctrl failed to report BSP version"
            logger.error(_err_msg)
            raise ValueError(_err_msg)

        bsp_ver_str = (
            f"r{ma.group('bsp_ver')}"  # NOTE: need to add 'r' prefix back here
        )
        bsp_ver = BSPVersion.parse(bsp_ver_str)
        if bsp_ver.major_rev == 0:
            _err_msg = f"invalid BSP version: {bsp_ver_str}, this might indicate broken firmware!"
            logger.warning(_err_msg)
            raise ValueError(_err_msg)
        return bsp_ver


EFIVARS_FSTYPE = "efivarfs"
EFIVARS_DPATH = "/sys/firmware/efi/efivars/"


class CapsuleUpdate:
    """Firmware update implementation using Capsule update."""

    def __init__(
        self,
        boot_parent_devpath: StrOrPath,
        standby_slot_mp: StrOrPath,
        *,
        ota_image_bsp_ver: BSPVersion,
        fw_bsp_ver_control: FirmwareBSPVersionControl,
    ) -> None:
        self.fw_bsp_ver_control = fw_bsp_ver_control
        self.ota_image_bsp_ver = ota_image_bsp_ver

        # NOTE: use the esp partition at the current booted device
        #   i.e., if we boot from nvme0n1, then bootdev_path is /dev/nvme0n1 and
        #   we use the esp at nvme0n1.
        self.esp_mp = Path(boot_cfg.ESP_MOUNTPOINT)
        self.esp_boot_dir = self.esp_mp / "EFI" / "BOOT"
        self.l4tlauncher_ver_fpath = self.esp_boot_dir / boot_cfg.L4TLAUNCHER_VER_FNAME
        self.bootaa64_at_esp = self.esp_boot_dir / boot_cfg.L4TLAUNCHER_FNAME
        self.bootaa64_at_esp_bak = (
            self.esp_boot_dir / f"{boot_cfg.L4TLAUNCHER_FNAME}_bak"
        )

        # NOTE: we get the update capsule from the standby slot
        self.standby_slot_mp = Path(standby_slot_mp)
        self.esp_part = self._detect_esp_dev(boot_parent_devpath)

    @staticmethod
    @contextlib.contextmanager
    def _ensure_efivarfs_mounted() -> Generator[None, Any, None]:
        """Ensure the efivarfs is mounted as rw."""
        if CMDHelperFuncs.is_target_mounted(EFIVARS_DPATH):
            options = "remount,rw,nosuid,nodev,noexec,relatime"
        else:
            logger.warning(
                f"efivars is not mounted! try to mount it at {EFIVARS_DPATH}"
            )
            options = "rw,nosuid,nodev,noexec,relatime"

        # fmt: off
        cmd = [
            "mount",
            "-t", EFIVARS_FSTYPE,
            "-o", options,
            EFIVARS_FSTYPE,
            EFIVARS_DPATH
        ]
        # fmt: on
        try:
            subprocess_call(cmd, raise_exception=True)
            yield
        except Exception as e:
            raise JetsonUEFIBootControlError(
                f"failed to mount {EFIVARS_FSTYPE} on {EFIVARS_DPATH}: {e!r}"
            ) from e

    @staticmethod
    @contextlib.contextmanager
    def _ensure_esp_mounted(
        esp_dev: StrOrPath, mount_point: StrOrPath
    ) -> Generator[None, Any, None]:
        """Mount the esp partition and then umount it."""
        mount_point = Path(mount_point)
        mount_point.mkdir(exist_ok=True, parents=True)

        try:
            CMDHelperFuncs.mount_rw(str(esp_dev), mount_point)
            yield
            CMDHelperFuncs.umount(mount_point, raise_exception=False)
        except Exception as e:
            _err_msg = f"failed to mount {esp_dev} to {mount_point}: {e!r}"
            logger.error(_err_msg)
            raise JetsonUEFIBootControlError(_err_msg) from e

    def _prepare_fwupdate_capsule(self) -> bool:
        """Copy the Capsule update payloads to specific location at esp partition.

        Returns:
            True if at least one of the update capsule is prepared, False if no update
                capsule is available and configured.
        """
        capsule_at_esp = self.esp_mp / boot_cfg.CAPSULE_PAYLOAD_AT_ESP
        capsule_at_esp.mkdir(parents=True, exist_ok=True)

        # where the fw update capsule and l4tlauncher bin located
        fw_loc_at_standby_slot = self.standby_slot_mp / Path(
            boot_cfg.CAPSULE_PAYLOAD_AT_ROOTFS
        ).relative_to("/")

        # ------ prepare capsule update payload ------ #
        firmware_package_configured = False
        for capsule_fname in boot_cfg.FIRMWARE_LIST:
            try:
                shutil.copy(
                    src=fw_loc_at_standby_slot / capsule_fname,
                    dst=capsule_at_esp / capsule_fname,
                )
                firmware_package_configured = True
                logger.info(f"copy {capsule_fname} to {capsule_at_esp}")
            except Exception as e:
                logger.warning(
                    f"failed to copy {capsule_fname} from {fw_loc_at_standby_slot} to {capsule_at_esp}: {e!r}"
                )
                logger.warning(f"skip {capsule_fname}")
        return firmware_package_configured

    def _update_l4tlauncher(self) -> bool:
        """update L4TLauncher with OTA image's one."""
        ota_image_l4tlauncher_ver = self.ota_image_bsp_ver
        logger.warning(f"update the l4tlauncher to version {ota_image_l4tlauncher_ver}")

        # new BOOTAA64.efi is located at /opt/ota_package/BOOTAA64.efi
        ota_image_bootaa64 = (
            self.standby_slot_mp
            / Path(boot_cfg.CAPSULE_PAYLOAD_AT_ROOTFS).relative_to("/")
            / boot_cfg.L4TLAUNCHER_FNAME
        )
        if not ota_image_bootaa64.is_file():
            logger.warning(f"{ota_image_bootaa64} not found, skip update l4tlauncher")
            return False

        shutil.copy(self.bootaa64_at_esp, self.bootaa64_at_esp_bak)
        shutil.copy(ota_image_bootaa64, self.bootaa64_at_esp)
        write_str_to_file_sync(
            self.l4tlauncher_ver_fpath, ota_image_l4tlauncher_ver.dump()
        )
        os.sync()
        return True

    @staticmethod
    def _write_magic_efivar() -> None:
        """Write magic efivar to trigger firmware Capsule update in next boot.

        Raises:
            JetsonUEFIBootControlError on failed Capsule update preparing.
        """
        magic_efivar_fpath = Path(EFIVARS_DPATH) / boot_cfg.UPDATE_TRIGGER_EFIVAR
        magic_efivar_fpath.write_bytes(boot_cfg.MAGIC_BYTES)
        os.sync()

    def _detect_l4tlauncher_version(self) -> BSPVersion:
        l4tlauncher_bsp_ver = None
        try:
            l4tlauncher_bsp_ver = BSPVersion.parse(
                self.l4tlauncher_ver_fpath.read_text()
            )
        except Exception as e:
            logger.warning(f"missing or invalid l4tlauncher version file: {e!r}")
            self.l4tlauncher_ver_fpath.unlink(missing_ok=True)

        bootaa64_at_esp = self.esp_boot_dir / boot_cfg.L4TLAUNCHER_FNAME
        # NOTE(20240624): since the number of l4tlauncher version is limited,
        #   we can lookup against a pre-calculated sha256 digest map.
        if l4tlauncher_bsp_ver is None:
            _l4tlauncher_sha256_digest = file_sha256(bootaa64_at_esp)
            logger.info(
                f"try to determine the l4tlauncher verison by hash: {_l4tlauncher_sha256_digest}"
            )
            l4tlauncher_bsp_ver = L4TLAUNCHER_BSP_VER_SHA256_MAP.get(
                _l4tlauncher_sha256_digest
            )

        current_slot_fw_bsp_ver = self.fw_bsp_ver_control.current_slot_fw_ver
        # NOTE(20240624): if we failed to detect the l4tlauncher's version,
        #   we assume that the launcher is the same version as current slot's fw.
        #   This is typically case for a newly OTA inital setup ECU.
        if l4tlauncher_bsp_ver is None:
            logger.warning(
                (
                    "failed to determine the l4tlauncher's version, assuming "
                    f"version is the same as current slot's fw version: {current_slot_fw_bsp_ver}"
                )
            )
            l4tlauncher_bsp_ver = current_slot_fw_bsp_ver
            write_str_to_file_sync(
                self.l4tlauncher_ver_fpath, l4tlauncher_bsp_ver.dump()
            )
        logger.info(f"finish detecting l4tlauncher version: {l4tlauncher_bsp_ver}")
        return l4tlauncher_bsp_ver

    @staticmethod
    def _detect_esp_dev(boot_parent_devpath: StrOrPath) -> str:
        # NOTE: if boots from external, expects to have multiple esp parts,
        #   we need to get the one at our booted parent dev.
        esp_parts = CMDHelperFuncs.get_dev_by_token(
            token="PARTLABEL", value=boot_cfg.ESP_PARTLABEL
        )
        if not esp_parts:
            raise JetsonUEFIBootControlError("no ESP partition presented")

        for _esp_part in esp_parts:
            if _esp_part.find(str(boot_parent_devpath)) != -1:
                logger.info(f"find esp partition at {_esp_part}")
                esp_part = _esp_part
                break
        else:
            _err_msg = f"failed to find esp partition on {boot_parent_devpath}"
            logger.error(_err_msg)
            raise JetsonUEFIBootControlError(_err_msg)
        return esp_part

    # APIs

    def firmware_update(self) -> bool:
        """Trigger firmware update in next boot.

        Returns:
            True if firmware update is configured, False if there is no firmware update.
        """
        standby_slot_fw_bsp_ver = self.fw_bsp_ver_control.standby_slot_fw_ver
        if (
            standby_slot_fw_bsp_ver
            and standby_slot_fw_bsp_ver >= self.ota_image_bsp_ver
        ):
            logger.info(
                (
                    "standby slot has newer or equal ver of firmware, skip firmware update: "
                    f"{standby_slot_fw_bsp_ver=}, {self.ota_image_bsp_ver=}"
                )
            )
            return False

        with self._ensure_esp_mounted(self.esp_part, self.esp_mp):
            if not self._prepare_fwupdate_capsule():
                logger.info("no firmware file is prepared, skip firmware update")
                return False

        with self._ensure_efivarfs_mounted():
            try:
                self._write_magic_efivar()
            except Exception as e:
                logger.warning(
                    (
                        f"failed to configure capsule update by write magic value: {e!r}\n"
                        "firmware update might be skipped!"
                    )
                )
                return False

        logger.info("firmware update package prepare finished")
        return True

    def l4tlauncher_update(self) -> bool:
        """Update l4tlauncher if needed.

        NOTE(20240611): Assume that the new L4TLauncher always keeps backward compatibility to
           work with old firmware. This assumption MUST be confirmed on the real ECU.
        NOTE(20240611): Only update l4tlauncher but never downgrade it.

        Returns:
            True if l4tlauncher is updated, else if there is no l4tlauncher update.
        """
        l4tlauncher_bsp_ver = self._detect_l4tlauncher_version()
        ota_image_l4tlauncher_ver = self.ota_image_bsp_ver
        if l4tlauncher_bsp_ver >= ota_image_l4tlauncher_ver:
            logger.info(
                (
                    "installed l4tlauncher has newer or equal version of l4tlauncher to OTA image's one, "
                    f"{l4tlauncher_bsp_ver=}, {ota_image_l4tlauncher_ver=}, "
                    "skip l4tlauncher update"
                )
            )
            return False

        with self._ensure_esp_mounted(self.esp_part, self.esp_mp):
            return self._update_l4tlauncher()


class _UEFIBoot:
    """Low-level boot control implementation for jetson-uefi."""

    _slot_id_partid = {SlotID("0"): "1", SlotID("1"): "2"}

    def __init__(self):
        # ------ sanity check, confirm we are at jetson device ------ #
        tegra_compat_info_fpath = Path(boot_cfg.TEGRA_COMPAT_PATH)
        if not tegra_compat_info_fpath.is_file():
            _err_msg = f"not a jetson device, {tegra_compat_info_fpath} doesn't exist"
            logger.error(_err_msg)
            raise JetsonUEFIBootControlError(_err_msg)

        compat_info = tegra_compat_info_fpath.read_text()
        # example compatible string:
        #   nvidia,p3737-0000+p3701-0000nvidia,tegra234nvidia,tegra23x
        if compat_info.find("tegra") == -1:
            _err_msg = f"uncompatible device: {compat_info=}"
            logger.error(_err_msg)
            raise JetsonUEFIBootControlError(_err_msg)
        logger.info(f"dev compatibility: {compat_info}")

        # ------ check BSP version ------ #
        # check firmware BSP version
        try:
            self.fw_bsp_version = fw_bsp_version = (
                _NVBootctrl.get_current_fw_bsp_version()
            )
            assert fw_bsp_version, "bsp version information not available"
            logger.info(f"current slot firmware BSP version: {fw_bsp_version}")
        except Exception as e:
            _err_msg = f"failed to detect BSP version: {e!r}"
            logger.error(_err_msg)
            raise JetsonUEFIBootControlError(_err_msg)
        logger.info(f"{fw_bsp_version=}")

        # check rootfs BSP version
        try:
            self.rootfs_bsp_verion = rootfs_bsp_version = parse_bsp_version(
                Path(boot_cfg.NV_TEGRA_RELEASE_FPATH).read_text()
            )
            logger.info(f"current slot rootfs BSP version: {rootfs_bsp_version}")
        except Exception as e:
            logger.warning(f"failed to detect rootfs BSP version: {e!r}")
            self.rootfs_bsp_verion = rootfs_bsp_version = None

        if rootfs_bsp_version and rootfs_bsp_version > fw_bsp_version:
            logger.warning(
                (
                    "current slot's rootfs bsp version is newer than the firmware bsp version, "
                    "this indicates the device previously only receive rootfs update, this is unexpected"
                )
            )

        # ------ sanity check, currently jetson-uefi only supports >= R35.2 ----- #
        # only after R35.2, the Capsule Firmware update is available.
        if fw_bsp_version < (35, 2, 0):
            _err_msg = f"jetson-uefi only supports BSP version >= R35.2, but get {fw_bsp_version=}. "
            logger.error(_err_msg)
            raise JetsonUEFIBootControlError(_err_msg)

        # unified A/B is enabled by default and cannot be disabled after BSP R34.
        logger.info("unified A/B is enabled")

        # ------ check A/B slots ------ #
        self.current_slot = current_slot = _NVBootctrl.get_current_slot()
        self.standby_slot = standby_slot = _NVBootctrl.get_standby_slot()
        logger.info(f"{current_slot=}, {standby_slot=}")

        # ------ detect rootfs_dev and parent_dev ------ #
        self.curent_rootfs_devpath = current_rootfs_devpath = (
            CMDHelperFuncs.get_current_rootfs_dev().strip()
        )
        self.parent_devpath = parent_devpath = Path(
            CMDHelperFuncs.get_parent_dev(current_rootfs_devpath).strip()
        )

        # --- detect boot device --- #
        self._external_rootfs = False
        parent_devname = parent_devpath.name
        if parent_devname.startswith(boot_cfg.MMCBLK_DEV_PREFIX):
            logger.info(f"device boots from internal emmc: {parent_devpath}")
        elif parent_devname.startswith(boot_cfg.NVMESSD_DEV_PREFIX):
            logger.info(f"device boots from external nvme ssd: {parent_devpath}")
            self._external_rootfs = True
        else:
            _err_msg = f"we don't support boot from {parent_devpath=} currently"
            logger.error(_err_msg)
            raise JetsonUEFIBootControlError(_err_msg) from NotImplementedError(
                f"unsupported bootdev {parent_devpath}"
            )

        self.standby_rootfs_devpath = (
            f"/dev/{parent_devname}p{self._slot_id_partid[standby_slot]}"
        )
        self.standby_rootfs_dev_partuuid = CMDHelperFuncs.get_attrs_by_dev(
            "PARTUUID", self.standby_rootfs_devpath
        ).strip()
        current_rootfs_dev_partuuid = CMDHelperFuncs.get_attrs_by_dev(
            "PARTUUID", current_rootfs_devpath
        ).strip()

        logger.info(
            "rootfs device: \n"
            f"active_rootfs(slot {current_slot}): {self.curent_rootfs_devpath=}, {current_rootfs_dev_partuuid=}\n"
            f"standby_rootfs(slot {standby_slot}): {self.standby_rootfs_devpath=}, {self.standby_rootfs_dev_partuuid=}"
        )

        self.standby_internal_emmc_devpath = f"/dev/{boot_cfg.INTERNAL_EMMC_DEVNAME}p{self._slot_id_partid[standby_slot]}"

        logger.info("finished jetson-uefi boot control startup")
        logger.info(f"nvbootctrl dump-slots-info: \n{_NVBootctrl.dump_slots_info()}")

    # API

    @property
    def external_rootfs_enabled(self) -> bool:
        """Indicate whether rootfs on external storage is enabled.

        NOTE: distiguish from boot from external storage, as R32.5 and below doesn't
            support native NVMe boot.
        """
        return self._external_rootfs

    def switch_boot_to_standby(self) -> None:
        target_slot = self.standby_slot

        logger.info(f"switch boot to standby slot({target_slot})")
        # when unified_ab enabled, switching bootloader slot will also switch
        #   the rootfs slot.
        _NVBootctrl.set_active_boot_slot(target_slot)


class JetsonUEFIBootControl(BootControllerProtocol):
    """BootControllerProtocol implementation for jetson-uefi."""

    def __init__(self) -> None:
        current_ota_status_dir = Path(boot_cfg.OTA_STATUS_DIR)
        current_ota_status_dir.mkdir(exist_ok=True, parents=True)
        standby_ota_status_dir = Path(cfg.MOUNT_POINT) / Path(
            boot_cfg.OTA_STATUS_DIR
        ).relative_to("/")

        # NOTE: this hint file is referred by finalize_switching_boot
        self.current_fwupdate_hint_fpath = (
            current_ota_status_dir / boot_cfg.FIRMWARE_UPDATE_HINT_FNAME
        )
        self.standby_fwupdate_hint_fpath = (
            standby_ota_status_dir / boot_cfg.FIRMWARE_UPDATE_HINT_FNAME
        )

        try:
            # startup boot controller
            self._uefi_control = uefi_control = _UEFIBoot()

            # mount point prepare
            self._mp_control = SlotMountHelper(
                standby_slot_dev=uefi_control.standby_rootfs_devpath,
                standby_slot_mount_point=cfg.MOUNT_POINT,
                active_slot_dev=self._uefi_control.curent_rootfs_devpath,
                active_slot_mount_point=cfg.ACTIVE_ROOT_MOUNT_POINT,
            )

            # load firmware BSP version
            self._current_fw_bsp_ver_fpath = (
                current_ota_status_dir / boot_cfg.FIRMWARE_BSP_VERSION_FNAME
            )
            # NOTE: standby slot's bsp version file might be not yet
            #   available before an OTA.
            self._standby_fw_bsp_ver_fpath = (
                standby_ota_status_dir / boot_cfg.FIRMWARE_BSP_VERSION_FNAME
            )
            self._firmware_ver_control = fw_bsp_ver = FirmwareBSPVersionControl(
                current_slot=uefi_control.current_slot,
                current_slot_firmware_bsp_ver=uefi_control.fw_bsp_version,
                current_firmware_bsp_vf=self._current_fw_bsp_ver_fpath,
            )

            # init ota-status files
            self._ota_status_control = OTAStatusFilesControl(
                active_slot=str(self._uefi_control.current_slot),
                standby_slot=str(self._uefi_control.standby_slot),
                current_ota_status_dir=current_ota_status_dir,
                # NOTE: might not yet be populated before OTA update applied!
                standby_ota_status_dir=standby_ota_status_dir,
                finalize_switching_boot=self._finalize_switching_boot,
            )

            # post starting up, write the firmware bsp version to current slot
            # NOTE 1: we always update and refer current slot's firmware bsp version file.
            # NOTE 2: if OTA status is failure, always assume the firmware update on standby slot failed,
            #   and clear the standby slot's fw bsp version record.
            if self._ota_status_control._ota_status == api_types.StatusOta.FAILURE:
                fw_bsp_ver.standby_slot_fw_ver = None
            fw_bsp_ver.current_slot_fw_ver = uefi_control.fw_bsp_version
            fw_bsp_ver.write_to_file(self._current_fw_bsp_ver_fpath)
        except Exception as e:
            _err_msg = f"failed to start jetson-uefi controller: {e!r}"
            raise ota_errors.BootControlStartupFailed(_err_msg, module=__name__) from e
        finally:
            # NOTE: the hint file is checked during OTAStatusFilesControl __init__,
            #   by finalize_switching_boot if we are in first reboot after OTA.
            #   once we have done parsing the hint file, we must remove it immediately.
            self.current_fwupdate_hint_fpath.unlink(missing_ok=True)

    def _finalize_switching_boot(self) -> bool:
        """Verify firmware update result and write firmware BSP version file.

        NOTE that if capsule firmware update failed, we must be booted back to the
            previous slot, so actually we don't need to do any checks here.
        """
        return True

    def _capsule_firmware_update(self) -> bool:
        """Perform firmware update with UEFI Capsule update if needed.

        If standby slot is known to have newer bootable firmware, skip firmware update.

        Returns:
            True if there is firmware update configured, False for no firmware update.
        """
        logger.info("jetson-uefi: checking if we need to do firmware update ...")

        standby_bootloader_slot = self._uefi_control.standby_slot

        # ------ check if we need to do firmware update ------ #
        skip_firmware_update_hint_file = (
            self._mp_control.standby_slot_mount_point
            / Path(boot_cfg.CAPSULE_PAYLOAD_AT_ROOTFS).relative_to("/")
            / Path(boot_cfg.NO_FIRMWARE_UPDATE_HINT_FNAME)
        )
        if skip_firmware_update_hint_file.is_file():
            logger.warning(
                "target image is configured to not doing firmware update, skip"
            )
            return False

        _new_bsp_v_fpath = self._mp_control.standby_slot_mount_point / Path(
            boot_cfg.NV_TEGRA_RELEASE_FPATH
        ).relative_to("/")
        try:
            new_bsp_v = parse_bsp_version(_new_bsp_v_fpath.read_text())
        except Exception as e:
            logger.warning(f"failed to detect new image's BSP version: {e!r}")
            logger.info("skip firmware update due to new image BSP version unknown")
            return False

        standby_firmware_bsp_ver = self._firmware_ver_control.standby_slot_fw_ver
        if standby_firmware_bsp_ver and standby_firmware_bsp_ver >= new_bsp_v:
            logger.info(
                f"{standby_bootloader_slot=} has newer or equal ver of firmware, skip firmware update"
            )
            return False

        # ------ prepare firmware update ------ #
        firmware_updater = CapsuleUpdate(
            boot_parent_devpath=self._uefi_control.parent_devpath,
            standby_slot_mp=self._mp_control.standby_slot_mount_point,
            ota_image_bsp_ver=new_bsp_v,
            fw_bsp_ver_control=self._firmware_ver_control,
        )
        if firmware_updater.firmware_update():
            firmware_updater.l4tlauncher_update()

            logger.info(
                (
                    f"will update to new firmware version in next reboot: {new_bsp_v=}, \n"
                    f"will switch to Slot({standby_bootloader_slot}) on successful firmware update"
                )
            )
            return True
        return False

    # APIs

    def get_standby_slot_path(self) -> Path:
        return self._mp_control.standby_slot_mount_point

    def get_standby_boot_dir(self) -> Path:
        return self._mp_control.standby_boot_dir

    def pre_update(self, version: str, *, standby_as_ref: bool, erase_standby: bool):
        try:
            logger.info("jetson-uefi: pre-update ...")
            # udpate active slot's ota_status
            self._ota_status_control.pre_update_current()

            # prepare standby slot dev
            self._mp_control.prepare_standby_dev(erase_standby=erase_standby)
            # mount slots
            self._mp_control.mount_standby()
            self._mp_control.mount_active()

            # update standby slot's ota_status files
            self._ota_status_control.pre_update_standby(version=version)
        except Exception as e:
            _err_msg = f"failed on pre_update: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.BootControlPreUpdateFailed(
                _err_msg, module=__name__
            ) from e

    def post_update(self) -> Generator[None, None, None]:
        try:
            logger.info("jetson-uefi: post-update ...")
            # ------ update extlinux.conf ------ #
            update_standby_slot_extlinux_cfg(
                active_slot_extlinux_fpath=Path(boot_cfg.EXTLINUX_FILE),
                standby_slot_extlinux_fpath=self._mp_control.standby_slot_mount_point
                / Path(boot_cfg.EXTLINUX_FILE).relative_to("/"),
                standby_slot_partuuid=self._uefi_control.standby_rootfs_dev_partuuid,
            )

            # ------ preserve BSP version file to standby slot ------ #
            self._firmware_ver_control.write_to_file(self._standby_fw_bsp_ver_fpath)

            # ------ preserve /boot/ota folder to standby rootfs ------ #
            preserve_ota_config_files_to_standby(
                active_slot_ota_dirpath=self._mp_control.active_slot_mount_point
                / "boot"
                / "ota",
                standby_slot_ota_dirpath=self._mp_control.standby_slot_mount_point
                / "boot"
                / "ota",
            )

            # ------ switch boot to standby ------ #
            firmware_update_triggered = self._capsule_firmware_update()
            # NOTE: manual switch boot will cancel the firmware update and cancel the switch boot itself!
            if not firmware_update_triggered:
                self._uefi_control.switch_boot_to_standby()
                logger.info(
                    f"no firmware update configured, manually switch slot: \n{_NVBootctrl.dump_slots_info()}"
                )

            # ------ for external rootfs, preserve /boot folder to internal ------ #
            # NOTE: the copy should happen AFTER the changes to /boot folder at active slot.
            if self._uefi_control._external_rootfs:
                logger.info(
                    "rootfs on external storage enabled: "
                    "copy standby slot rootfs' /boot folder "
                    "to corresponding internal emmc dev ..."
                )
                copy_standby_slot_boot_to_internal_emmc(
                    internal_emmc_mp=Path(boot_cfg.SEPARATE_BOOT_MOUNT_POINT),
                    internal_emmc_devpath=self._uefi_control.standby_internal_emmc_devpath,
                    standby_slot_boot_dirpath=self._mp_control.standby_slot_mount_point
                    / "boot",
                )

            # ------ prepare to reboot ------ #
            self._mp_control.umount_all(ignore_error=True)
            logger.info("post update finished, wait for reboot ...")
            yield  # hand over control back to otaclient
            CMDHelperFuncs.reboot()
        except Exception as e:
            _err_msg = f"jetson-uefi: failed on post_update: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.BootControlPostUpdateFailed(
                _err_msg, module=__name__
            ) from e

    def pre_rollback(self):
        try:
            logger.info("jetson-uefi: pre-rollback setup ...")
            self._ota_status_control.pre_rollback_current()
            self._mp_control.mount_standby()
            self._ota_status_control.pre_rollback_standby()
        except Exception as e:
            _err_msg = f"jetson-uefi: failed on pre_rollback: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.BootControlPreRollbackFailed(
                _err_msg, module=__name__
            ) from e

    def post_rollback(self):
        try:
            logger.info("jetson-uefi: post-rollback setup...")
            self._mp_control.umount_all(ignore_error=True)
            self._uefi_control.switch_boot_to_standby()
            CMDHelperFuncs.reboot()
        except Exception as e:
            _err_msg = f"jetson-uefi: failed on post_rollback: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.BootControlPostRollbackFailed(
                _err_msg, module=__name__
            ) from e

    def on_operation_failure(self):
        """Failure registering and cleanup at failure."""
        logger.warning("on failure try to unmounting standby slot...")
        self._ota_status_control.on_failure()
        self._mp_control.umount_all(ignore_error=True)

    def load_version(self) -> str:
        return self._ota_status_control.load_active_slot_version()

    def get_booted_ota_status(self) -> api_types.StatusOta:
        return self._ota_status_control.booted_ota_status
