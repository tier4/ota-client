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

jetson-uefi module currently support BSP version >= R34(which UEFI is introduced).
But firmware update is only supported after BSP R35.2.
"""


from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, ClassVar, Generator, Literal

from pydantic import BaseModel
from typing_extensions import Self

from otaclient.app import errors as ota_errors
from otaclient.app.configs import config as cfg
from otaclient.boot_control._firmware_package import (
    DigestValue,
    FirmwareManifest,
    FirmwareUpdateRequest,
    PayloadType,
    load_firmware_package,
)
from otaclient_api.v2 import types as api_types
from otaclient_common import replace_root
from otaclient_common.common import file_sha256, subprocess_call, write_str_to_file_sync
from otaclient_common.typing import StrOrPath

from ._common import CMDHelperFuncs, OTAStatusFilesControl, SlotMountHelper
from ._jetson_common import (
    SLOT_PAR_MAP,
    BSPVersion,
    FirmwareBSPVersionControl,
    NVBootctrlCommon,
    copy_standby_slot_boot_to_internal_emmc,
    detect_external_rootdev,
    detect_rootfs_bsp_version,
    get_nvbootctrl_conf_tnspec,
    get_partition_devpath,
    preserve_ota_config_files_to_standby,
    update_standby_slot_extlinux_cfg,
)
from .configs import jetson_uefi_cfg as boot_cfg
from .protocol import BootControllerProtocol

logger = logging.getLogger(__name__)

MINIMUM_SUPPORTED_BSP_VERSION = BSPVersion(34, 0, 0)
"""Only after R34, UEFI is introduced."""
FIRMWARE_UPDATE_MINIMUM_SUPPORTED_BSP_VERSION = BSPVersion(35, 2, 0)
"""Only after R35.2, UEFI Capsule firmware update is introduced."""

L4TLAUNCHER_BSP_VER_SHA256_MAP: dict[str, BSPVersion] = {
    "b14fa3623f4078d05573d9dcf2a0b46ea2ae07d6b75d9843f9da6ff24db13718": BSPVersion(
        36, 3, 0
    ),
    "3928e0feb84e37db87dc6705a02eec95a6fca2dccd3467b61fa66ed8e1046b67": BSPVersion(
        35, 5, 0
    ),
    "ac17457772666351154a5952e3b87851a6398da2afcf3a38bedfc0925760bb0e": BSPVersion(
        35, 4, 1
    ),
}


class JetsonUEFIBootControlError(Exception):
    """Exception type for covering jetson-uefi related errors."""


class NVBootctrlJetsonUEFI(NVBootctrlCommon):
    """Helper for calling nvbootctrl commands, for platform using jetson-uefi.

    Typical output of nvbootctrl dump-slots-info:

    ```
    Current version: 35.4.1
    Capsule update status: 1
    Current bootloader slot: A
    Active bootloader slot: A
    num_slots: 2
    slot: 0,             status: normal
    slot: 1,             status: normal
    ```

    For BSP version >= R34 with UEFI boot.
    Without -t option, the target will be bootloader by default.
    """

    @classmethod
    def get_current_fw_bsp_version(cls) -> BSPVersion:
        """Get current boot chain's firmware BSP version with nvbootctrl."""
        _raw = cls.dump_slots_info()
        pa = re.compile(r"Current version:\s*(?P<bsp_ver>[\.\d]+)")

        if not (ma := pa.search(_raw)):
            _err_msg = "nvbootctrl failed to report BSP version"
            logger.error(_err_msg)
            raise JetsonUEFIBootControlError(_err_msg)

        bsp_ver_str = ma.group("bsp_ver")
        bsp_ver = BSPVersion.parse(bsp_ver_str)
        if bsp_ver.major_rev == 0:
            _err_msg = f"invalid BSP version: {bsp_ver_str}, this might indicate an incomplete flash!"
            logger.error(_err_msg)
            raise ValueError(_err_msg)
        return bsp_ver

    @classmethod
    def verify(cls) -> str:
        """Verify the bootloader and rootfs boot."""
        return cls._nvbootctrl("verify", check_output=True)


EFIVARS_FSTYPE = "efivarfs"
EFIVARS_SYS_MOUNT_POINT = "/sys/firmware/efi/efivars/"


@contextlib.contextmanager
def _ensure_efivarfs_mounted() -> Generator[None, Any, None]:
    """Ensure the efivarfs is mounted as rw, and then umount it."""
    if CMDHelperFuncs.is_target_mounted(EFIVARS_SYS_MOUNT_POINT):
        options = "remount,rw,nosuid,nodev,noexec,relatime"
    else:
        logger.warning(
            f"efivars is not mounted! try to mount it at {EFIVARS_SYS_MOUNT_POINT}"
        )
        options = "rw,nosuid,nodev,noexec,relatime"

    # fmt: off
    cmd = [
        "mount",
        "-t", EFIVARS_FSTYPE,
        "-o", options,
        EFIVARS_FSTYPE,
        EFIVARS_SYS_MOUNT_POINT
    ]
    # fmt: on
    try:
        subprocess_call(cmd, raise_exception=True)
        yield
    except Exception as e:
        raise JetsonUEFIBootControlError(
            f"failed to mount {EFIVARS_FSTYPE} on {EFIVARS_SYS_MOUNT_POINT}: {e!r}"
        ) from e


def _trigger_capsule_update_qspi_ota_bootdev() -> bool:
    """Write magic efivar to trigger firmware Capsule update in next boot.

    This method is for device using QSPI flash as TEGRA_OTA_BOOT_DEVICE.
    """
    with _ensure_efivarfs_mounted():
        try:
            magic_efivar_fpath = (
                Path(EFIVARS_SYS_MOUNT_POINT) / boot_cfg.UPDATE_TRIGGER_EFIVAR
            )
            magic_efivar_fpath.write_bytes(boot_cfg.MAGIC_BYTES)
            os.sync()

            return True
        except Exception as e:
            logger.warning(
                (
                    f"failed to configure capsule update by write magic value: {e!r}\n"
                    "firmware update might be skipped!"
                )
            )
            return False


def _trigger_capsule_update_non_qspi_ota_bootdev(esp_mp: StrOrPath) -> bool:
    """Write magic efivar to trigger firmware Capsule update in next boot.

    Write a special file with magic value into specific location at internal emmc ESP partition.

    This method is for device NOT using QSPI flash as TEGRA_OTA_BOOT_DEVICE.
    """
    uefi_variable_dir = Path(esp_mp) / "EFI/NVDA/Variables"
    magic_variable_fpath = uefi_variable_dir / boot_cfg.UPDATE_TRIGGER_EFIVAR

    try:
        emmc_esp_partition = _detect_esp_dev(f"/dev/{boot_cfg.INTERNAL_EMMC_DEVNAME}")
        with _ensure_esp_mounted(emmc_esp_partition, esp_mp):
            magic_variable_fpath.write_bytes(boot_cfg.MAGIC_BYTES)
            os.sync()

        return True
    except Exception as e:
        logger.warning(
            (
                f"failed to configure capsule update: {e!r}\n"
                "firmware update might be skipped!"
            )
        )
        return False


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


def _detect_esp_dev(boot_parent_devpath: StrOrPath) -> str:
    # NOTE: if boots from external, expects to have multiple esp parts,
    #   we need to get the one at our booted parent dev.
    esp_parts = CMDHelperFuncs.get_dev_by_token(
        token="PARTLABEL", value=boot_cfg.ESP_PARTLABEL
    )
    if not esp_parts:
        raise JetsonUEFIBootControlError("no ESP partition presented")

    for _esp_part in esp_parts:
        if _esp_part.strip().startswith(str(boot_parent_devpath)):
            logger.info(f"find esp partition at {_esp_part}")
            esp_part = _esp_part
            break
    else:
        _err_msg = f"failed to find esp partition on {boot_parent_devpath}"
        logger.error(_err_msg)
        raise JetsonUEFIBootControlError(_err_msg)
    return esp_part


def _detect_ota_bootdev_is_qspi(nvboot_ctrl_conf: str) -> bool | None:
    """Detect whether the device is using QSPI or not.

    This is required for determining the way to trigger UEFI Capsule update.
    If the TEGRA_OTA_BOOT_DEVICE is mtdblock device, then the device is using QSPI.
    If is mmcblk0bootx, then the device is using internal emmc.

    If we cannot determine the TEGRA_OTA_BOOT_DEVICE, we MUST NOT execute the firmware update.

    Returns:
        True if device is using QSPI flash, False for device is using internal emmc,
            None for unrecognized device or missing TEGRA_OTA_BOOT_DEVICE field in conf.
    """
    for line in nvboot_ctrl_conf.splitlines():
        if line.strip().startswith("TEGRA_OTA_BOOT_DEVICE"):
            _, _ota_bootdev = line.split(" ", maxsplit=1)
            ota_bootdev = _ota_bootdev.strip()
            break
    else:
        logger.warning("no TEGRA_OTA_BOOT_DEVICE field found in nv_boot_ctrl.conf")
        return

    if ota_bootdev.startswith("/dev/mmcblk0"):
        logger.info("device is using internal emmc flash as TEGRA_OTA_BOOT_DEVICE")
        return False

    if ota_bootdev.startswith("/dev/mtdblock0"):
        logger.info("device uses QSPI flash as TEGRA_OTA_BOOT_DEVICE")
        return True

    logger.warning(f"device uses unknown TEGRA_OTA_BOOT_DEVICE: {ota_bootdev}")


class L4TLauncherBSPVersionControl(BaseModel):
    """Track the L4TLauncher binary BSP version.

    Schema: <bsp_ver_str>:<sha256_digest>
    """

    bsp_ver: BSPVersion
    sha256_digest: str
    SEP: ClassVar[Literal[":"]] = ":"

    @classmethod
    def parse(cls, _in: str) -> Self:
        bsp_str, digest = _in.strip().split(":")
        return cls(bsp_ver=BSPVersion.parse(bsp_str), sha256_digest=digest)

    def dump(self) -> str:
        return f"{self.bsp_ver.dump()}{self.SEP}{self.sha256_digest}"


class UEFIFirmwareUpdater:
    """Firmware update implementation using Capsule update."""

    def __init__(
        self,
        boot_parent_devpath: StrOrPath,
        standby_slot_mp: StrOrPath,
        *,
        nvbootctrl_conf: str,
        fw_bsp_ver_control: FirmwareBSPVersionControl,
        firmware_update_request: FirmwareUpdateRequest,
        firmware_manifest: FirmwareManifest,
    ) -> None:
        """Init an instance of UEFIFirmwareUpdater.

        Args:
            boot_parent_devpath (StrOrPath): The parent dev of current rootfs device.
            standby_slot_mp (StrOrPath): The mount point of updated standby slot. The firmware update package
                is located at <standby_slot_mp>/opt/ota_package folder.
            nvbootctrl_conf (str): The contents of nv_boot_ctrl.conf file.
            fw_bsp_ver_control (FirmwareBSPVersionControl): The firmware BSP version control for each slots.
            firmware_update_request (FirmwareUpdateRequest)
            firmware_manifest (FirmwareBSPVersionControl)
        """
        self.current_slot_bsp_ver = fw_bsp_ver_control.current_slot_bsp_ver
        self.standby_slot_bsp_ver = fw_bsp_ver_control.standby_slot_bsp_ver

        self.nvbootctrl_conf = nvbootctrl_conf
        self.firmware_update_request = firmware_update_request
        self.firmware_manifest = firmware_manifest
        self.firmware_package_bsp_ver = BSPVersion.parse(
            firmware_manifest.firmware_spec.bsp_version
        )

        self.esp_part = _detect_esp_dev(boot_parent_devpath)
        # NOTE: use the esp partition at the current booted device
        #   i.e., if we boot from nvme0n1, then bootdev_path is /dev/nvme0n1 and
        #   we use the esp at nvme0n1.
        self.esp_mp = Path(boot_cfg.ESP_MOUNTPOINT)
        self.esp_mp.mkdir(exist_ok=True)

        esp_boot_dir = self.esp_mp / "EFI" / "BOOT"
        self.l4tlauncher_ver_fpath = esp_boot_dir / boot_cfg.L4TLAUNCHER_VER_FNAME
        """A plain text file stores the BSP version string."""
        self.bootaa64_at_esp = esp_boot_dir / boot_cfg.L4TLAUNCHER_FNAME
        """The canonical location of L4TLauncher, called by UEFI."""
        self.bootaa64_at_esp_bak = esp_boot_dir / f"{boot_cfg.L4TLAUNCHER_FNAME}_bak"
        """The location to backup current L4TLauncher binary."""
        self.capsule_dir_at_esp = self.esp_mp / boot_cfg.CAPSULE_PAYLOAD_AT_ESP
        """The location to put UEFI capsule to. The UEFI will use the capsule in this location."""

        self.standby_slot_mp = Path(standby_slot_mp)

    def _prepare_fwupdate_capsule(self) -> bool:
        """Copy the Capsule update payloads to specific location at esp partition.

        The UEFI framework will look for the firmware update capsule there and start
            the firmware update.

        Returns:
            True if at least one of the update capsule is prepared, False if no update
                capsule is available and configured.
        """
        capsule_dir_at_esp = self.capsule_dir_at_esp
        capsule_dir_at_esp.mkdir(parents=True, exist_ok=True)

        # ------ prepare capsule update payload ------ #
        firmware_package_configured = False
        for capsule_payload in self.firmware_manifest.get_firmware_packages(
            self.firmware_update_request
        ):
            if capsule_payload.type != PayloadType.UEFI_CAPSULE:
                continue

            # NOTE: currently we only support payload indicated by file path.
            capsule_flocation = capsule_payload.file_location
            assert capsule_flocation.location_type == "file"
            capsule_fpath = capsule_flocation.location_path

            try:
                shutil.copy(
                    src=replace_root(capsule_fpath, "/", self.standby_slot_mp),
                    dst=capsule_dir_at_esp / capsule_payload.payload_name,
                )
                firmware_package_configured = True
                logger.info(
                    f"copy {capsule_payload.payload_name} to {capsule_dir_at_esp}"
                )
            except Exception as e:
                logger.warning(
                    f"failed to copy {capsule_payload.payload_name} to {capsule_dir_at_esp}: {e!r}"
                )
                logger.warning(f"skip preparing {capsule_payload.payload_name}")
        return firmware_package_configured

    def _update_l4tlauncher(self) -> bool:
        """update L4TLauncher with OTA image's one."""
        for capsule_payload in self.firmware_manifest.get_firmware_packages(
            self.firmware_update_request
        ):
            if capsule_payload.type != PayloadType.UEFI_BOOT_APP:
                continue

            logger.warning(
                f"update the l4tlauncher to version {self.firmware_package_bsp_ver} ..."
            )

            # NOTE: currently we only support payload indicated by file path.
            bootapp_fpath = capsule_payload.file_location
            assert not isinstance(bootapp_fpath, DigestValue)

            # new BOOTAA64.efi is located at /opt/ota_package/BOOTAA64.efi
            ota_image_bootaa64 = replace_root(bootapp_fpath, "/", self.standby_slot_mp)
            try:
                shutil.copy(self.bootaa64_at_esp, self.bootaa64_at_esp_bak)
                shutil.copy(ota_image_bootaa64, self.bootaa64_at_esp)
                os.sync()  # ensure the boot application is written to the disk

                write_str_to_file_sync(
                    self.l4tlauncher_ver_fpath, self.firmware_package_bsp_ver.dump()
                )
                return True
            except Exception as e:
                _err_msg = f"failed to prepare boot application update: {e!r}, skip"
                logger.warning(_err_msg)

        logger.info("no boot application update is configured in the request, skip")
        return False

    def _detect_l4tlauncher_version(self) -> BSPVersion:
        """Try to determine the current in use l4tlauncher version."""
        l4tlauncher_sha256_digest = file_sha256(self.bootaa64_at_esp)

        # try to determine the version with version file
        try:
            _ver_control = L4TLauncherBSPVersionControl.parse(
                self.l4tlauncher_ver_fpath.read_text()
            )
            if l4tlauncher_sha256_digest == _ver_control.sha256_digest:
                return _ver_control.bsp_ver

            logger.warning(
                (
                    "l4tlauncher sha256 hash mismatched: "
                    f"{l4tlauncher_sha256_digest=}, {_ver_control.sha256_digest=}, "
                    "remove version file"
                )
            )
            raise ValueError("sha256 hash mismatched")
        except Exception as e:
            logger.warning(f"missing or invalid l4tlauncher version file: {e!r}")
            self.l4tlauncher_ver_fpath.unlink(missing_ok=True)

        # try to determine the version by looking up table
        # NOTE(20240624): since the number of l4tlauncher version is limited,
        #   we can lookup against a pre-calculated sha256 digest map.
        logger.info(
            f"try to determine the l4tlauncher verison by hash: {l4tlauncher_sha256_digest}"
        )
        if l4tlauncher_bsp_ver := L4TLAUNCHER_BSP_VER_SHA256_MAP.get(
            l4tlauncher_sha256_digest
        ):
            _ver_control = L4TLauncherBSPVersionControl(
                bsp_ver=l4tlauncher_bsp_ver, sha256_digest=l4tlauncher_sha256_digest
            )
            write_str_to_file_sync(self.l4tlauncher_ver_fpath, _ver_control.dump())
            return l4tlauncher_bsp_ver

        # NOTE(20240624): if we failed to detect the l4tlauncher's version,
        #   we assume that the launcher is the same version as current slot's fw.
        #   This is typically the case of a newly flashed ECU.
        logger.warning(
            (
                "failed to determine the l4tlauncher's version, assuming "
                f"version is the same as current slot's fw version: {self.current_slot_bsp_ver}"
            )
        )
        _ver_control = L4TLauncherBSPVersionControl(
            bsp_ver=self.current_slot_bsp_ver, sha256_digest=l4tlauncher_sha256_digest
        )
        write_str_to_file_sync(self.l4tlauncher_ver_fpath, _ver_control.dump())
        return self.current_slot_bsp_ver

    # APIs

    def firmware_update(self) -> bool:
        """Trigger firmware update in next boot if configured.

        Only when the following conditions met the firmware update will be configured:
        1. a firmware_update_request file is presented and valid in the OTA image.
        2. the firmware_manifest is presented and valid in the OTA image.
        3. the firmware package is compatible with the device.
        4. firmware specified in firmware_update_request is valid.
        5. the standby slot's firmware is older than the OTA image's one.

        Returns:
            True if firmware update is configured, False if there is no firmware update.
        """
        # sanity check, UEFI Capsule firmware update is only supported after R35.2.
        if (
            self.standby_slot_bsp_ver
            and self.standby_slot_bsp_ver
            < FIRMWARE_UPDATE_MINIMUM_SUPPORTED_BSP_VERSION
        ):
            _err_msg = (
                f"UEFI Capsule firmware update is introduced since {FIRMWARE_UPDATE_MINIMUM_SUPPORTED_BSP_VERSION}, "
                f"but get {self.standby_slot_bsp_ver}, abort"
            )
            logger.error(_err_msg)
            return False

        # check BSP version, NVIDIA Jetson device with R34 or newer doesn't allow firmware downgrade.
        if (
            self.standby_slot_bsp_ver
            and self.standby_slot_bsp_ver > self.firmware_package_bsp_ver
        ):
            logger.info(
                (
                    "standby slot has newer ver of firmware, skip firmware update: "
                    f"{self.standby_slot_bsp_ver=}, {self.firmware_package_bsp_ver=}"
                )
            )
            return False

        # check firmware compatibility, this is to prevent failed firmware update beforehand.
        tnspec = get_nvbootctrl_conf_tnspec(self.nvbootctrl_conf)
        if not tnspec:
            logger.warning(
                "TNSPEC is not defined in nvbootctrl config file, skip firmware update!"
            )
            return False

        if not self.firmware_manifest.check_compat(tnspec):
            _err_msg = (
                "firmware package is incompatible with this device: "
                f"{tnspec=}, {self.firmware_manifest.firmware_spec.firmware_compat}, "
                "skip firmware update"
            )
            logger.warning(_err_msg)
            return False

        # copy the firmware update capsule to specific location
        with _ensure_esp_mounted(self.esp_part, self.esp_mp):
            if not self._prepare_fwupdate_capsule():
                logger.info("no firmware file is prepared, skip firmware update")
                return False

            logger.info("on capsule prepared, try to update L4TLauncher ...")
            l4tlauncher_bsp_ver = self._detect_l4tlauncher_version()
            logger.info(f"current l4tlauncher version: {l4tlauncher_bsp_ver}")

            if l4tlauncher_bsp_ver >= self.firmware_package_bsp_ver:
                logger.info(
                    (
                        "installed l4tlauncher has newer or equal version of l4tlauncher to OTA image's one, "
                        f"{l4tlauncher_bsp_ver=}, {self.firmware_package_bsp_ver=}, "
                        "skip l4tlauncher update"
                    )
                )
            else:
                self._update_l4tlauncher()

        # write special UEFI variable to trigger firmware update on next reboot
        firmware_update_triggerred = False
        if _detect_ota_bootdev_is_qspi(self.nvbootctrl_conf):
            firmware_update_triggerred = _trigger_capsule_update_qspi_ota_bootdev()
        else:
            firmware_update_triggerred = _trigger_capsule_update_non_qspi_ota_bootdev(
                self.esp_mp
            )

        if firmware_update_triggerred:
            logger.warning(
                "firmware update package prepare finished"
                f"will update firmware to {self.firmware_package_bsp_ver} in next reboot"
            )
        return firmware_update_triggerred


class _UEFIBootControl:
    """Low-level boot control implementation for jetson-uefi."""

    def __init__(self):
        # ------ sanity check, confirm we are at jetson device ------ #
        tegra_compat_info_fpath = Path(boot_cfg.TEGRA_COMPAT_PATH)
        if not tegra_compat_info_fpath.is_file():
            _err_msg = f"not a jetson device, {tegra_compat_info_fpath} doesn't exist"
            logger.error(_err_msg)
            raise JetsonUEFIBootControlError(_err_msg)

        # print hardware model
        if (model_fpath := Path(boot_cfg.MODEL_FPATH)).is_file():
            logger.info(f"hardware model: {model_fpath.read_text()}")

        compat_info = tegra_compat_info_fpath.read_text()
        # example compatible string:
        #   nvidia,p3737-0000+p3701-0000nvidia,tegra234nvidia,tegra23x
        if compat_info.find("tegra") == -1:
            _err_msg = f"uncompatible device: {compat_info=}"
            logger.error(_err_msg)
            raise JetsonUEFIBootControlError(_err_msg)
        logger.info(f"dev compatibility: {compat_info}")

        # load nvbootctrl config file
        if not (
            nvbootctrl_conf_fpath := Path(boot_cfg.NVBOOTCTRL_CONF_FPATH)
        ).is_file():
            _err_msg = "nv_boot_ctrl.conf is missing!"
            logger.error(_err_msg)
            raise JetsonUEFIBootControlError(_err_msg)
        self.nvbootctrl_conf = nvbootctrl_conf_fpath.read_text()
        logger.info(f"nvboot_ctrl_conf: \n{self.nvbootctrl_conf}")

        # ------ check current slot BSP version ------ #
        # check current slot firmware BSP version
        try:
            self.fw_bsp_version = fw_bsp_version = (
                NVBootctrlJetsonUEFI.get_current_fw_bsp_version()
            )
            assert fw_bsp_version, "bsp version information not available"
            logger.info(f"current slot firmware BSP version: {fw_bsp_version}")
        except Exception as e:
            _err_msg = f"failed to detect BSP version: {e!r}"
            logger.error(_err_msg)
            raise JetsonUEFIBootControlError(_err_msg)
        logger.info(f"{fw_bsp_version=}")

        # check current slot rootfs BSP version
        try:
            self.rootfs_bsp_verion = rootfs_bsp_version = detect_rootfs_bsp_version(
                rootfs=cfg.ACTIVE_ROOTFS_PATH
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

        if rootfs_bsp_version != fw_bsp_version:
            logger.warning(
                (
                    f"current slot has rootfs BSP version {rootfs_bsp_version}, "
                    f"while the firmware version is {fw_bsp_version}, "
                    "rootfs BSP version and firmware version are MISMATCHED! "
                    "This might result in nvbootctrl not working as expected!"
                )
            )

        # ------ sanity check, jetson-uefi only supports >= R34 ----- #
        if fw_bsp_version < MINIMUM_SUPPORTED_BSP_VERSION:
            _err_msg = f"jetson-uefi only supports BSP version >= {MINIMUM_SUPPORTED_BSP_VERSION}, but get {fw_bsp_version=}"
            logger.error(_err_msg)
            raise JetsonUEFIBootControlError(_err_msg)

        # unified A/B is enabled by default and cannot be disabled after BSP R34.
        logger.info("unified A/B is enabled")

        # ------ check A/B slots ------ #
        self.current_slot = current_slot = NVBootctrlJetsonUEFI.get_current_slot()
        self.standby_slot = standby_slot = NVBootctrlJetsonUEFI.get_standby_slot()
        logger.info(f"{current_slot=}, {standby_slot=}")

        # ------ detect rootfs_dev and parent_dev ------ #
        self.curent_rootfs_devpath = current_rootfs_devpath = (
            CMDHelperFuncs.get_current_rootfs_dev()
        )
        self.parent_devpath = parent_devpath = Path(
            CMDHelperFuncs.get_parent_dev(current_rootfs_devpath)
        )

        # --- detect boot device --- #
        self.external_rootfs = detect_external_rootdev(parent_devpath)

        self.standby_rootfs_devpath = get_partition_devpath(
            parent_devpath=parent_devpath,
            partition_id=SLOT_PAR_MAP[standby_slot],
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

        self.standby_internal_emmc_devpath = get_partition_devpath(
            parent_devpath=f"/dev/{boot_cfg.INTERNAL_EMMC_DEVNAME}",
            partition_id=SLOT_PAR_MAP[standby_slot],
        )

        logger.info("finished jetson-uefi boot control startup")
        logger.info(
            f"nvbootctrl dump-slots-info: \n{NVBootctrlJetsonUEFI.dump_slots_info()}"
        )

    # API

    def switch_boot_to_standby(self) -> None:
        target_slot = self.standby_slot

        logger.info(f"switch boot to standby slot({target_slot})")
        # when unified_ab enabled, switching bootloader slot will also switch
        #   the rootfs slot.
        NVBootctrlJetsonUEFI.set_active_boot_slot(target_slot)


class JetsonUEFIBootControl(BootControllerProtocol):
    """BootControllerProtocol implementation for jetson-uefi."""

    def __init__(self) -> None:
        try:
            self._uefi_control = uefi_control = _UEFIBootControl()

            # mount point prepare
            self._mp_control = SlotMountHelper(
                standby_slot_dev=uefi_control.standby_rootfs_devpath,
                standby_slot_mount_point=cfg.MOUNT_POINT,
                active_slot_dev=self._uefi_control.curent_rootfs_devpath,
                active_slot_mount_point=cfg.ACTIVE_ROOT_MOUNT_POINT,
            )

            # init ota-status files
            current_ota_status_dir = Path(boot_cfg.OTA_STATUS_DIR)
            standby_ota_status_dir = Path(
                replace_root(
                    boot_cfg.OTA_STATUS_DIR,
                    "/",
                    cfg.MOUNT_POINT,
                )
            )
            self._ota_status_control = OTAStatusFilesControl(
                active_slot=str(self._uefi_control.current_slot),
                standby_slot=str(self._uefi_control.standby_slot),
                current_ota_status_dir=current_ota_status_dir,
                # NOTE: might not yet be populated before OTA update applied!
                standby_ota_status_dir=standby_ota_status_dir,
                finalize_switching_boot=self._finalize_switching_boot,
            )

            # load firmware BSP version
            current_fw_bsp_ver_fpath = (
                current_ota_status_dir / boot_cfg.FIRMWARE_BSP_VERSION_FNAME
            )
            self._firmware_bsp_ver_control = bsp_ver_ctrl = FirmwareBSPVersionControl(
                current_slot=uefi_control.current_slot,
                current_slot_bsp_ver=uefi_control.fw_bsp_version,
                current_bsp_version_file=current_fw_bsp_ver_fpath,
            )
            # always update the bsp_version_file on startup to reflect
            #   the up-to-date current slot BSP version
            self._firmware_bsp_ver_control.write_to_file(current_fw_bsp_ver_fpath)
            logger.info(
                f"\ncurrent slot firmware BSP version: {uefi_control.fw_bsp_version}\n"
                f"standby slot firmware BSP version: {bsp_ver_ctrl.standby_slot_bsp_ver}"
            )

            logger.info("jetson-uefi boot control start up finished")
        except Exception as e:
            _err_msg = f"failed to start jetson-uefi controller: {e!r}"
            raise ota_errors.BootControlStartupFailed(_err_msg, module=__name__) from e

    def _finalize_switching_boot(self) -> bool:
        """Verify firmware update result and write firmware BSP version file.

        NOTE that if capsule firmware update failed, we must be booted back to the
            previous slot, so actually we don't need to do any checks here.
        """
        try:
            fw_update_verify = NVBootctrlJetsonUEFI.verify()
            logger.info(f"nvbootctrl verify: {fw_update_verify}")
        except Exception as e:
            logger.warning(f"nvbootctrl verify failed: {e!r}")
        return True

    def _firmware_update(self) -> bool:
        """Perform firmware update with UEFI Capsule update if needed.

        Returns:
            True if there is firmware update configured, False for no firmware update.
        """
        logger.info("jetson-uefi: checking if we need to do firmware update ...")
        firmware_package_meta = load_firmware_package(
            firmware_update_request_fpath=replace_root(
                boot_cfg.FIRMWARE_UPDATE_REQUEST_FPATH,
                "/",
                self._mp_control.standby_slot_mount_point,
            ),
            firmware_manifest_fpath=replace_root(
                boot_cfg.FIRMWARE_MANIFEST_FPATH,
                "/",
                self._mp_control.standby_slot_mount_point,
            ),
        )
        if firmware_package_meta is None:
            logger.info("skip firmware update ...")
            return False
        firmware_update_request, firmware_manifest = firmware_package_meta

        fw_update_bsp_ver = BSPVersion.parse(
            firmware_manifest.firmware_spec.bsp_version
        )
        logger.info(f"firmware update package BSP version: {fw_update_bsp_ver}")

        # ------ prepare firmware update ------ #
        firmware_updater = UEFIFirmwareUpdater(
            boot_parent_devpath=self._uefi_control.parent_devpath,
            standby_slot_mp=self._mp_control.standby_slot_mount_point,
            fw_bsp_ver_control=self._firmware_bsp_ver_control,
            firmware_update_request=firmware_update_request,
            firmware_manifest=firmware_manifest,
            nvbootctrl_conf=self._uefi_control.nvbootctrl_conf,
        )
        return firmware_updater.firmware_update()

    # APIs

    def get_standby_slot_path(self) -> Path:
        return self._mp_control.standby_slot_mount_point

    def get_standby_boot_dir(self) -> Path:
        return self._mp_control.standby_boot_dir

    def pre_update(self, version: str, *, standby_as_ref: bool, erase_standby: bool):
        try:
            logger.info("jetson-uefi: pre-update ...")
            self._ota_status_control.pre_update_current()

            self._mp_control.prepare_standby_dev(erase_standby=erase_standby)
            self._mp_control.mount_standby()
            self._mp_control.mount_active()

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
            standby_fw_bsp_ver_fpath = (
                self._ota_status_control.standby_ota_status_dir
                / boot_cfg.FIRMWARE_BSP_VERSION_FNAME
            )
            self._firmware_bsp_ver_control.write_to_file(standby_fw_bsp_ver_fpath)

            # ------ preserve /boot/ota folder to standby rootfs ------ #
            preserve_ota_config_files_to_standby(
                active_slot_ota_dirpath=self._mp_control.active_slot_mount_point
                / "boot"
                / "ota",
                standby_slot_ota_dirpath=self._mp_control.standby_slot_mount_point
                / "boot"
                / "ota",
            )

            # ------ firmware update & switch boot to standby ------ #
            firmware_update_triggered = self._firmware_update()
            # NOTE: manual switch boot will cancel the scheduled firmware update!
            if not firmware_update_triggered:
                self._uefi_control.switch_boot_to_standby()
                logger.info(
                    f"no firmware update configured, manually switch slot: \n{NVBootctrlJetsonUEFI.dump_slots_info()}"
                )

            # ------ for external rootfs, preserve /boot folder to internal ------ #
            # NOTE: the copy should happen AFTER the changes to /boot folder at active slot.
            if self._uefi_control.external_rootfs:
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
