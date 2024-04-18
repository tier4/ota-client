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
import logging
import os
import re
import shutil
from functools import partial
from pathlib import Path
from typing import Generator, Literal

from otaclient.app import errors as ota_errors
from otaclient.app.common import copytree_identical, write_str_to_file_sync
from otaclient.app.proto import wrapper
from ._common import (
    OTAStatusFilesControl,
    SlotMountHelper,
    CMDHelperFuncs,
)
from .configs import cboot_cfg as cfg
from ._jetson_common import (
    FirmwareBSPVersionControl,
    NVBootctrlCommon,
    SlotID,
    parse_bsp_version,
)
from .protocol import BootControllerProtocol

logger = logging.getLogger(__name__)


class JetsonUEFIBootControlError(Exception):
    """Exception type for covering jetson-uefi related errors."""


class _NVBootctrl(NVBootctrlCommon):
    """Helper for calling nvbootctrl commands.

    For BSP version >= R34 with UEFI boot.
    Without -t option, the target will be bootloader by default.
    """

    NVBOOTCTRL = "nvbootctrl"
    NVBootctrlTarget = Literal["bootloader", "rootfs"]

    @classmethod
    def get_capsule_update_result(cls) -> str:
        """Check the Capsule update status.

        NOTE: this is NOT a nvbootctrl command, but implemented by parsing
            the result of calling nvbootctrl dump-slots-info.

        The output value of Capsule update status can be following:
            0 - No Capsule update
            1 - Capsule update successfully
            2 - Capsule install successfully but boot new firmware failed
            3 - Capsule install failed

        Returns:
            The Capsulte update result status.
        """
        slots_info = cls.dump_slots_info()
        logger.info(f"checking Capsule update result: \n{slots_info}")

        pattern = re.compile(r"Capsule update status: (?P<status>\d+)")
        ma = pattern.search(slots_info)
        assert ma, "failed to get Capsule update result"

        update_result = ma.group("status")
        return update_result


class CapsuleUpdate:
    """Firmware update implementation using Capsule update."""

    CAPSULE_PAYLOAD_LOCATION = "EFI/UpdateCapsule"
    MAGIC_BYTES = b"\x07\x00\x00\x00\x04\x00\x00\x00\x00\x00\x00\x00"
    UPDATE_TRIGGER_EFIVAR = "OsIndications-8be4df61-93ca-11d2-aa0d-00e098032b8c"
    EFIVARS_DPATH = "/sys/firmware/efi/efivars/"
    EFIVARS_FSTYPE = "efivarfs"
    ESP_PARTLABEL = "esp"
    ESP_MOUNTPOINT = "/mnt/esp"
    # TODO: move these options into uefi boot configs
    CAPSULE_PAYLOAD_FNAME = "bl_only_payload.Cap"

    def __init__(self, boot_devpath: Path | str) -> None:
        # NOTE: use the esp partition at the current booted device
        #   i.e., if we boot from nvme0n1, then bootdev_path is /dev/nvme0n1 and
        #   we use the esp at nvme0n1.
        self.boot_devpath = Path(boot_devpath)
        self.boot_devname = Path(boot_devpath).name

        self.esp_mp = self.ESP_MOUNTPOINT

        # if boots from external, expects to have multiple esp parts, we need to get the one at our booted dev
        esp_parts = CMDHelperFuncs.get_dev_by_token(
            token="PARTLABEL", value=self.ESP_PARTLABEL
        )
        for _esp_part in esp_parts:
            if _esp_part.find(self.boot_devname) != -1:
                logger.info(f"find esp partition at {_esp_part}")
                self.esp_part = _esp_part
                break
        else:
            _err_msg = f"failed to find esp partition on {self.boot_devpath}"
            logger.error(_err_msg)
            raise JetsonUEFIBootControlError(_err_msg)

    @classmethod
    def _ensure_efivarfs_mounted(cls) -> None:
        if CMDHelperFuncs.is_target_mounted(cls.EFIVARS_DPATH):
            return

        logger.warning(
            f"efivars is not mounted! try to mount it at {cls.EFIVARS_DPATH}"
        )
        try:
            CMDHelperFuncs._mount(
                cls.EFIVARS_FSTYPE,
                cls.EFIVARS_DPATH,
                fstype=cls.EFIVARS_FSTYPE,
                options=["rw", "nosuid", "nodev", "noexec", "relatime"],
            )
        except Exception as e:
            raise JetsonUEFIBootControlError(
                f"failed to mount {cls.EFIVARS_FSTYPE} on {cls.EFIVARS_DPATH}: {e!r}"
            ) from e

    def _prepare_payload(self) -> None:
        """Copy the Capsule update payload to the specified location."""
        try:
            CMDHelperFuncs.mount_rw(self.esp_part, self.esp_mp)
        except Exception as e:
            raise JetsonUEFIBootControlError(
                f"failed to mount {self.esp_part} onto {self.esp_mp}: {e!r}"
            )

        capsule_payload_fpath = Path(cfg.FIRMWARE_DPATH) / self.CAPSULE_PAYLOAD_FNAME
        capsule_payload_location = Path(self.esp_mp) / self.CAPSULE_PAYLOAD_LOCATION
        try:
            shutil.copy(capsule_payload_fpath, capsule_payload_location)
        except Exception as e:
            _err_msg = f"failed to copy update Capsule from {capsule_payload_fpath} to {capsule_payload_location}: {e!r}"
            logger.error(_err_msg)
            raise JetsonUEFIBootControlError(_err_msg) from e
        finally:
            CMDHelperFuncs.umount(self.esp_mp, ignore_error=True)

    def _write_efivar(self) -> None:
        """Write magic efivar to trigger firmware Capsule update in next boot."""
        magic_efivar_fpath = Path(self.EFIVARS_DPATH) / self.UPDATE_TRIGGER_EFIVAR
        try:
            with open(magic_efivar_fpath, "rb") as f:
                f.write(self.MAGIC_BYTES)
                os.fsync(f.fileno())
        except Exception as e:
            _err_msg = f"failed to write magic bytes into {magic_efivar_fpath}: {e!r}"
            logger.error(_err_msg)
            raise JetsonUEFIBootControlError(_err_msg) from e

    def firmware_update(self) -> None:
        """Trigger firmware update in next boot."""
        logger.info("prepare for firmware Capsule update ...")
        self._ensure_efivarfs_mounted()
        self._prepare_payload()
        self._write_efivar()
        logger.info(
            "firmware Capsule update is configured and will be triggerred in next boot"
        )


class _UEFIBoot:
    """Low-level boot control implementation for jetson-uefi."""

    MMCBLK_DEV_PREFIX = "mmcblk"  # internal emmc
    NVMESSD_DEV_PREFIX = "nvme"  # external nvme ssd
    INTERNAL_EMMC_DEVNAME = "mmcblk0"
    _slot_id_partid = {SlotID("0"): "1", SlotID("1"): "2"}

    def __init__(self):
        # ------ sanity check, confirm we are at jetson device ------ #
        if not os.path.exists(cfg.TEGRA_CHIP_ID_PATH):
            _err_msg = f"not a jetson device, {cfg.TEGRA_CHIP_ID_PATH} doesn't exist"
            logger.error(_err_msg)
            raise JetsonUEFIBootControlError(_err_msg)

        # ------ check BSP version ------ #
        try:
            self.bsp_version = bsp_version = parse_bsp_version(
                Path(cfg.NV_TEGRA_RELEASE_FPATH).read_text()
            )
        except Exception as e:
            _err_msg = f"failed to detect BSP version: {e!r}"
            logger.error(_err_msg)
            raise JetsonUEFIBootControlError(_err_msg)
        logger.info(f"{bsp_version=}")

        # ------ sanity check, currently jetson-uefi only supports >= R35.2 ----- #
        if not bsp_version >= (35, 2, 0):
            _err_msg = f"jetson-uefi only supports BSP version >= R35.2, but get {bsp_version=}. "
            logger.error(_err_msg)
            raise JetsonUEFIBootControlError(_err_msg)

        # NOTE: unified A/B is enabled by default and cannot be disabled after BSP R34.
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

        self._external_rootfs = False
        parent_devname = parent_devpath.name
        if parent_devname.startswith(self.MMCBLK_DEV_PREFIX):
            logger.info(f"device boots from internal emmc: {parent_devpath}")
        elif parent_devname.startswith(self.NVMESSD_DEV_PREFIX):
            logger.info(f"device boots from external nvme ssd: {parent_devpath}")
            self._external_rootfs = True
        else:
            _err_msg = f"we don't support boot from {parent_devpath=} currently"
            logger.error(_err_msg)
            raise JetsonUEFIBootControlError(_err_msg) from NotImplementedError(
                f"unsupported bootdev {parent_devpath}"
            )

        # rootfs partition
        self.standby_rootfs_devpath = (
            f"/dev/{parent_devname}p{self._slot_id_partid[standby_slot]}"
        )
        self.standby_rootfs_dev_partuuid = CMDHelperFuncs.get_partuuid_by_dev(
            f"{self.standby_rootfs_devpath}"
        ).strip()
        current_rootfs_dev_partuuid = CMDHelperFuncs.get_partuuid_by_dev(
            current_rootfs_devpath
        ).strip()

        logger.info(
            "rootfs device: \n"
            f"active_rootfs(slot {current_slot}): {self.curent_rootfs_devpath=}, {current_rootfs_dev_partuuid=}\n"
            f"standby_rootfs(slot {standby_slot}): {self.standby_rootfs_devpath=}, {self.standby_rootfs_dev_partuuid=}"
        )

        # internal emmc partition
        self.standby_internal_emmc_devpath = (
            f"/dev/{self.INTERNAL_EMMC_DEVNAME}p{self._slot_id_partid[standby_slot]}"
        )

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

    def prepare_standby_dev(self, *, erase_standby: bool):
        CMDHelperFuncs.umount(self.standby_rootfs_devpath, ignore_error=True)

        if erase_standby:
            try:
                CMDHelperFuncs.mkfs_ext4(self.standby_rootfs_devpath)
            except Exception as e:
                _err_msg = f"failed to mkfs.ext4 on standby dev: {e!r}"
                logger.error(_err_msg)
                raise JetsonUEFIBootControlError(_err_msg) from e
        # TODO: in the future if in-place update mode is implemented, do a
        #   fschck over the standby slot file system.

    @staticmethod
    def update_extlinux_cfg(_input: str, partuuid: str) -> str:
        """Update input exlinux text with input rootfs <partuuid_str>."""

        partuuid_str = f"PARTUUID={partuuid}"

        def _replace(ma: re.Match, repl: str):
            append_l: str = ma.group(0)
            if append_l.startswith("#"):
                return append_l
            res, n = re.compile(r"root=[\w\-=]*").subn(repl, append_l)
            if not n:  # this APPEND line doesn't contain root= placeholder
                res = f"{append_l} {repl}"

            return res

        _repl_func = partial(_replace, repl=f"root={partuuid_str}")
        return re.compile(r"\n\s*APPEND.*").sub(_repl_func, _input)


class JetsonUEFIBootControl(BootControllerProtocol):
    """BootControllerProtocol implementation for jetson-uefi."""

    def __init__(self) -> None:
        try:
            # startup boot controller
            self._uefi_control = _UEFIBoot()

            # mount point prepare
            self._mp_control = SlotMountHelper(
                standby_slot_dev=self._uefi_control.standby_rootfs_devpath,
                standby_slot_mount_point=cfg.MOUNT_POINT,
                active_slot_dev=self._uefi_control.curent_rootfs_devpath,
                active_slot_mount_point=cfg.ACTIVE_ROOT_MOUNT_POINT,
            )
            current_ota_status_dir = Path(cfg.OTA_STATUS_DIR)
            standby_ota_status_dir = self._mp_control.standby_slot_mount_point / Path(
                cfg.OTA_STATUS_DIR
            ).relative_to("/")

            # load firmware BSP version from current rootfs slot
            self._firmware_ver_control = FirmwareBSPVersionControl(
                current_firmware_bsp_vf=current_ota_status_dir
                / cfg.FIRMWARE_BSP_VERSION_FNAME,
                standby_firmware_bsp_vf=standby_ota_status_dir
                / cfg.FIRMWARE_BSP_VERSION_FNAME,
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
        except Exception as e:
            _err_msg = f"failed to start jetson-uefi controller: {e!r}"
            raise ota_errors.BootControlStartupFailed(_err_msg, module=__name__) from e

    def _finalize_switching_boot(self) -> bool:
        """Verify firmware update result and write firmware BSP version file.

        NOTE: due to unified A/B is enabled, actually it is impossible to boot to
            a firmware updated failed slot.
            Since finalize_switching_boot is only called when first reboot succeeds,
            we can only observe result status 0 or 1 here.
        """
        current_slot = self._uefi_control.current_slot
        current_slot_bsp_ver = self._uefi_control.bsp_version

        try:
            update_result_status = _NVBootctrl.get_capsule_update_result()
        except Exception as e:
            _err_msg = (
                f"failed to get the Capsule update result status, assume failed: {e!r}"
            )
            logger.error(_err_msg)
            return False

        if update_result_status == "0":
            logger.info("no firmware update occurs")
            return True

        if update_result_status == "1":
            logger.info("firmware successfully updated")
            self._firmware_ver_control.set_version_by_slot(
                current_slot, current_slot_bsp_ver
            )
            self._firmware_ver_control.write_current_firmware_bsp_version()
            return True

        return False

    def _copy_standby_slot_boot_to_internal_emmc(self):
        """Copy the standby slot's /boot to internal emmc dev.

        This method is involved when external rootfs is enabled, aligning with
            the behavior of the NVIDIA flashing script.

        WARNING: DO NOT call this method if we are not booted from external rootfs!
        NOTE: at the time this method is called, the /boot folder at
            standby slot rootfs MUST be fully setup!
        """
        # mount corresponding internal emmc device
        internal_emmc_mp = Path(cfg.SEPARATE_BOOT_MOUNT_POINT)
        internal_emmc_mp.mkdir(exist_ok=True, parents=True)
        internal_emmc_devpath = self._uefi_control.standby_internal_emmc_devpath

        try:
            CMDHelperFuncs.umount(internal_emmc_devpath, ignore_error=True)
            CMDHelperFuncs.mount_rw(internal_emmc_devpath, internal_emmc_mp)
        except Exception as e:
            _msg = f"failed to mount standby internal emmc dev: {e!r}"
            logger.error(_msg)
            raise JetsonUEFIBootControlError(_msg) from e

        try:
            dst = internal_emmc_mp / "boot"
            src = self._mp_control.standby_slot_mount_point / "boot"
            # copy the standby slot's boot folder to emmc boot dev
            copytree_identical(src, dst)
        except Exception as e:
            _msg = f"failed to populate standby slot's /boot folder to standby internal emmc dev: {e!r}"
            logger.error(_msg)
            raise JetsonUEFIBootControlError(_msg) from e
        finally:
            CMDHelperFuncs.umount(internal_emmc_mp, ignore_error=True)

    def _preserve_ota_config_files_to_standby(self):
        """Preserve /boot/ota to standby /boot folder."""
        src = self._mp_control.active_slot_mount_point / "boot" / "ota"
        if not src.is_dir():  # basically this should not happen
            logger.warning(f"{src} doesn't exist, skip preserve /boot/ota folder.")
            return

        dst = self._mp_control.standby_slot_mount_point / "boot" / "ota"
        # TODO: (20240411) reconsidering should we preserve /boot/ota?
        copytree_identical(src, dst)

    def _update_standby_slot_extlinux_cfg(self):
        """update standby slot's /boot/extlinux/extlinux.conf to update root indicator."""
        src = standby_slot_extlinux = self._mp_control.standby_slot_mount_point / Path(
            cfg.EXTLINUX_FILE
        ).relative_to("/")
        # if standby slot doesn't have extlinux.conf installed, use current booted
        #   extlinux.conf as template source.
        if not standby_slot_extlinux.is_file():
            src = Path(cfg.EXTLINUX_FILE)

        # update the extlinux.conf with standby slot rootfs' partuuid
        write_str_to_file_sync(
            standby_slot_extlinux,
            self._uefi_control.update_extlinux_cfg(
                src.read_text(),
                self._uefi_control.standby_rootfs_dev_partuuid,
            ),
        )

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
            self._uefi_control.prepare_standby_dev(erase_standby=erase_standby)
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
            self._update_standby_slot_extlinux_cfg()

            # ------ firmware update ------ #
            firmware_updater = CapsuleUpdate(self._uefi_control.parent_devpath)
            firmware_updater.firmware_update()

            # ------ preserve /boot/ota folder to standby rootfs ------ #
            self._preserve_ota_config_files_to_standby()

            # ------ for external rootfs, preserve /boot folder to internal ------ #
            if self._uefi_control._external_rootfs:
                logger.info(
                    "rootfs on external storage enabled: "
                    "copy standby slot rootfs' /boot folder "
                    "to corresponding internal emmc dev ..."
                )
                self._copy_standby_slot_boot_to_internal_emmc()

            # ------ switch boot to standby ------ #
            self._uefi_control.switch_boot_to_standby()

            # ------ prepare to reboot ------ #
            self._mp_control.umount_all(ignore_error=True)
            logger.info(f"[post-update]: \n{_NVBootctrl.dump_slots_info()}")
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

    def get_booted_ota_status(self) -> wrapper.StatusOta:
        return self._ota_status_control.booted_ota_status
