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


from __future__ import annotations
import os.path
from enum import Enum
from pydantic import BaseModel, ConfigDict
from typing import ClassVar, Dict

from otaclient._utils import cached_computed_field
from otaclient._utils.path import replace_root
from ..configs import config as cfg


class BootloaderType(str, Enum):
    """Bootloaders that supported by otaclient.

    grub: generic x86_64 platform with grub
    cboot: ADLink rqx-580, rqx-58g, with BSP 32.5.x
        (theoretically other Nvidia jetson xavier devices using cboot are also supported)
    rpi_boot: raspberry pi 4 with eeprom version newer than 2020-10-28
    """

    UNSPECIFIED = "unspecified"
    GRUB = "grub"
    CBOOT = "cboot"
    RPI_BOOT = "rpi_boot"

    @classmethod
    def parse_str(cls, _input: str) -> "BootloaderType":
        res = cls.UNSPECIFIED
        try:  # input is enum key(capitalized)
            res = BootloaderType[_input]
        except KeyError:
            pass
        try:  # input is enum value(uncapitalized)
            res = BootloaderType(_input)
        except ValueError:
            pass
        return res


class _SeparatedBootParOTAStatusConfig(BaseModel):
    """Configs for platforms that have separated boot partition.

    Currently cboot and rpi_boot are included in this catagory.

    grub is special as it uses shared /boot, so it doesn't use this config.
    """

    @cached_computed_field
    def ACTIVE_BOOT_OTA_STATUS_DPATH(self) -> str:
        return os.path.join(cfg.BOOT_DPATH, "ota-status")

    @cached_computed_field
    def STANDBY_BOOT_OTA_STATUS_DPATH(self) -> str:
        return replace_root(
            self.ACTIVE_BOOT_OTA_STATUS_DPATH, cfg.ACTIVE_ROOTFS, cfg.STANDBY_SLOT_MP
        )


class _CommonConfig(BaseModel):
    model_config = ConfigDict(frozen=True, validate_default=True)
    DEFAULT_FSTAB_FPATH: ClassVar[str] = "/etc/fstab"

    @cached_computed_field
    def STANDBY_FSTAB_FPATH(self) -> str:
        return replace_root(
            self.DEFAULT_FSTAB_FPATH, cfg.DEFAULT_ACTIVE_ROOTFS, cfg.STANDBY_SLOT_MP
        )

    @cached_computed_field
    def ACTIVE_FSTAB_FPATH(self) -> str:
        return replace_root(
            self.DEFAULT_FSTAB_FPATH, cfg.DEFAULT_ACTIVE_ROOTFS, cfg.ACTIVE_ROOTFS
        )


class GrubControlConfig(_CommonConfig):
    """x86-64 platform, with grub as bootloader."""

    BOOTLOADER: ClassVar[BootloaderType] = BootloaderType.GRUB
    GRUB_CFG_FNAME: ClassVar[str] = "grub.cfg"
    BOOT_OTA_PARTITION_FILE: ClassVar[str] = "ota-partition"

    @cached_computed_field
    def BOOT_GRUB_DPATH(self) -> str:
        return os.path.join(cfg.BOOT_DPATH, "grub")

    @cached_computed_field
    def GRUB_CFG_FPATH(self) -> str:
        return os.path.join(self.BOOT_GRUB_DPATH, self.GRUB_CFG_FNAME)

    @cached_computed_field
    def GRUB_DEFAULT_FPATH(self) -> str:
        return os.path.join(cfg.ETC_DPATH, "default/grub")


class CBootControlConfig(_CommonConfig, _SeparatedBootParOTAStatusConfig):
    """arm platform, with cboot as bootloader.

    NOTE: only for tegraid:0x19, roscube-x platform(jetson-xavier-agx series)
    """

    BOOTLOADER: ClassVar[BootloaderType] = BootloaderType.CBOOT
    CHIP_ID_MODEL_MAP: ClassVar[Dict[int, str]] = {0x19: "rqx_580"}
    DEFAULT_TEGRA_CHIP_ID_FPATH: ClassVar[
        str
    ] = "/sys/module/tegra_fuse/parameters/tegra_chip_id"
    DEFAULT_EXTLINUX_DPATH: ClassVar[str] = "/boot/extlinux"
    DEFAULT_FIRMWARE_CFG_FPATH: ClassVar[str] = "/opt/ota/firmwares/firmware.yaml"
    EXTLINUX_CFG_FNAME: ClassVar[str] = "extlinux.conf"

    @cached_computed_field
    def TEGRA_CHIP_ID_FPATH(self) -> str:
        return replace_root(
            self.DEFAULT_TEGRA_CHIP_ID_FPATH,
            cfg.DEFAULT_ACTIVE_ROOTFS,
            cfg.ACTIVE_ROOTFS,
        )

    @cached_computed_field
    def STANDBY_BOOT_EXTLINUX_DPATH(self) -> str:
        return replace_root(
            self.DEFAULT_EXTLINUX_DPATH,
            cfg.DEFAULT_ACTIVE_ROOTFS,
            cfg.STANDBY_SLOT_MP,
        )

    @cached_computed_field
    def STANDBY_EXTLINUX_FPATH(self) -> str:
        return os.path.join(self.STANDBY_BOOT_EXTLINUX_DPATH, self.EXTLINUX_CFG_FNAME)

    @cached_computed_field
    def SEPARATE_BOOT_MOUNT_POINT(self) -> str:
        return os.path.join(cfg.OTACLIENT_MOUNT_SPACE_DPATH, "standby_boot")

    # refer to the standby slot
    @cached_computed_field
    def FIRMWARE_CFG_STANDBY_FPATH(self) -> str:
        return replace_root(
            self.DEFAULT_FIRMWARE_CFG_FPATH,
            cfg.DEFAULT_ACTIVE_ROOTFS,
            cfg.STANDBY_SLOT_MP,
        )


class RPIBootControlConfig(_CommonConfig, _SeparatedBootParOTAStatusConfig):
    BBOOTLOADER: ClassVar[BootloaderType] = BootloaderType.RPI_BOOT
    DEFAULT_RPI_MODEL_FPATH: ClassVar[str] = "/proc/device-tree/model"
    RPI_MODEL_HINT: ClassVar[str] = "Raspberry Pi 4 Model B"
    SLOT_A_FSLABEL: ClassVar[str] = "slot_a"
    SLOT_B_FSLABEL: ClassVar[str] = "slot_b"
    SYSTEM_BOOT_FSLABEL: ClassVar[str] = "system-boot"
    SWITCH_BOOT_FLAG_FNAME: ClassVar[str] = "._ota_switch_boot_finalized"

    @cached_computed_field
    def RPI_MODEL_FPATH(self) -> str:
        return replace_root(
            self.DEFAULT_RPI_MODEL_FPATH,
            cfg.DEFAULT_ACTIVE_ROOTFS,
            cfg.ACTIVE_ROOTFS,
        )

    @cached_computed_field
    def SYSTEM_BOOT_MOUNT_POINT(self) -> str:
        return os.path.join(cfg.BOOT_DPATH, "firmware")

    @cached_computed_field
    def SWITCH_BOOT_FLAG_FPATH(self) -> str:
        return os.path.join(self.SYSTEM_BOOT_MOUNT_POINT, self.SWITCH_BOOT_FLAG_FNAME)


grub_cfg = GrubControlConfig()
cboot_cfg = CBootControlConfig()
rpi_boot_cfg = RPIBootControlConfig()
