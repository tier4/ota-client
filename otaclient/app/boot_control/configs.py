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
from enum import Enum, unique
from pydantic import BaseModel, ConfigDict
from typing import TYPE_CHECKING, ClassVar, Any
from typing_extensions import Self

from otaclient._utils import cached_computed_field
from otaclient._utils.path import replace_root
from ..configs import config as cfg

# A simple trick to make plain ClassVar work when
# __future__.annotations are activated.
if not TYPE_CHECKING:
    ClassVar = ClassVar[Any]


@unique
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
    def parse_str(cls, _input: str) -> Self:
        res = cls.UNSPECIFIED
        try:  # input is enum key(capitalized)
            res = cls[_input]
        except KeyError:
            pass
        try:  # input is enum value(uncapitalized)
            res = cls(_input)
        except ValueError:
            pass
        return res


class _SeparatedBootParOTAStatusConfig(BaseModel):
    """Configs for platforms that have separated boot dir.

    Currently cboot and rpi_boot platforms are included in this catagory.
    1. cboot: separated boot devices, boot dirs on each slots,
    2. rpi_boot: separated boot dirs on each slots, only share system-boot.

    grub platform shares /boot dir by sharing the same boot device.
    """

    @cached_computed_field
    def ACTIVE_BOOT_OTA_STATUS_DPATH(self) -> str:
        """The dynamically rooted location of ota-status dir.

        Default: /boot/ota-status
        """
        return os.path.join(cfg.BOOT_DPATH, "ota-status")

    @cached_computed_field
    def STANDBY_BOOT_OTA_STATUS_DPATH(self) -> str:
        """The dynamically rooted location standby slot's ota-status dir.

        NOTE: this location is relatived to standby slot's mount point.
        NOTE(20231117): for platform with separated boot dev(like cboot), it is boot controller's
            responsibility to copy the /boot dir from standby slot rootfs to separated boot dev.

        Default: /mnt/otaclient/standby_slot/boot/ota-status
        """
        return replace_root(
            self.ACTIVE_BOOT_OTA_STATUS_DPATH, cfg.ACTIVE_ROOTFS, cfg.STANDBY_SLOT_MP
        )


class _CommonConfig(BaseModel):
    model_config = ConfigDict(frozen=True, validate_default=True)
    DEFAULT_FSTAB_FPATH: ClassVar = "/etc/fstab"

    @cached_computed_field
    def STANDBY_FSTAB_FPATH(self) -> str:
        """The dynamically rooted location of standby slot's fstab file.

        NOTE: this location is relatived to standby slot's mount point.

        Default: /mnt/otaclient/standby_slot/etc/fstab
        """
        return replace_root(
            self.DEFAULT_FSTAB_FPATH, cfg.DEFAULT_ACTIVE_ROOTFS, cfg.STANDBY_SLOT_MP
        )

    @cached_computed_field
    def ACTIVE_FSTAB_FPATH(self) -> str:
        """The dynamically rooted location of active slot's fstab file.

        Default: /etc/fstab
        """
        return replace_root(
            self.DEFAULT_FSTAB_FPATH, cfg.DEFAULT_ACTIVE_ROOTFS, cfg.ACTIVE_ROOTFS
        )


class GrubControlConfig(_CommonConfig):
    """x86-64 platform, with grub as bootloader."""

    BOOTLOADER: ClassVar = BootloaderType.GRUB
    GRUB_CFG_FNAME: ClassVar = "grub.cfg"
    BOOT_OTA_PARTITION_FNAME: ClassVar = "ota-partition"

    @cached_computed_field
    def BOOT_GRUB_DPATH(self) -> str:
        """The dynamically rooted location of /boot/grub dir.

        Default: /boot/grub
        """
        return os.path.join(cfg.BOOT_DPATH, "grub")

    @cached_computed_field
    def GRUB_CFG_FPATH(self) -> str:
        """The dynamically rooted location of /boot/grub/grub.cfg file.

        Default: /boot/grub/grub.cfg
        """
        return os.path.join(self.BOOT_GRUB_DPATH, self.GRUB_CFG_FNAME)

    @cached_computed_field
    def GRUB_DEFAULT_FPATH(self) -> str:
        """The dynamically rooted location of /etc/default/grub file.

        Default: /etc/default/grub
        """
        return os.path.join(cfg.ETC_DPATH, "default/grub")


class CBootControlConfig(_CommonConfig, _SeparatedBootParOTAStatusConfig):
    """arm platform, with cboot as bootloader.

    NOTE: only for tegraid:0x19, roscube-x platform(jetson-xavier-agx series)
    """

    BOOTLOADER: ClassVar = BootloaderType.CBOOT
    CHIP_ID_MODEL_MAP: ClassVar = {0x19: "rqx_580"}
    DEFAULT_TEGRA_CHIP_ID_FPATH: ClassVar = (
        "/sys/module/tegra_fuse/parameters/tegra_chip_id"
    )
    DEFAULT_EXTLINUX_DPATH: ClassVar = "/boot/extlinux"
    DEFAULT_FIRMWARE_CFG_FPATH: ClassVar = "/opt/ota/firmwares/firmware.yaml"
    EXTLINUX_CFG_FNAME: ClassVar = "extlinux.conf"

    @cached_computed_field
    def TEGRA_CHIP_ID_FPATH(self) -> str:
        """The dynamically rooted location of tegra chip id query API.

        Default: /sys/module/tegra_fuse/parameters/tegra_chip_id
        """
        return replace_root(
            self.DEFAULT_TEGRA_CHIP_ID_FPATH,
            cfg.DEFAULT_ACTIVE_ROOTFS,
            cfg.ACTIVE_ROOTFS,
        )

    @cached_computed_field
    def STANDBY_BOOT_EXTLINUX_DPATH(self) -> str:
        """The dynamically rooted location of standby slot's extlinux cfg dir.

        NOTE: this location is relatived to standby slot's mount point.

        Default: /mnt/otaclient/standby_slot/boot/extlinux
        """
        return replace_root(
            self.DEFAULT_EXTLINUX_DPATH,
            cfg.DEFAULT_ACTIVE_ROOTFS,
            cfg.STANDBY_SLOT_MP,
        )

    @cached_computed_field
    def STANDBY_EXTLINUX_FPATH(self) -> str:
        """The dynamically rooted location of standby slot's extlinux cfg file.

        NOTE: this location is relatived to standby slot's mount point.

        Default: /mnt/otaclient/standby_slot/boot/extlinux/extlinux.conf
        """
        return os.path.join(self.STANDBY_BOOT_EXTLINUX_DPATH, self.EXTLINUX_CFG_FNAME)

    @cached_computed_field
    def SEPARATE_BOOT_MOUNT_POINT(self) -> str:
        """The dynamically rooted location of standby slot's boot dev mount point.

        Default: /mnt/otaclient/standby_root
        """
        return os.path.join(cfg.OTACLIENT_MOUNT_SPACE_DPATH, "standby_boot")

    # refer to the standby slot
    @cached_computed_field
    def FIRMWARE_CFG_STANDBY_FPATH(self) -> str:
        """The dynamically rooted location of standby slot's cboot firmware.yaml file.

        NOTE: this location is relatived to standby slot's mount point.

        Default: /mnt/otaclient/standby_slot/opt/ota/firmwares/firmware.yaml
        """
        return replace_root(
            self.DEFAULT_FIRMWARE_CFG_FPATH,
            cfg.DEFAULT_ACTIVE_ROOTFS,
            cfg.STANDBY_SLOT_MP,
        )


class RPIBootControlConfig(_CommonConfig, _SeparatedBootParOTAStatusConfig):
    BBOOTLOADER: ClassVar = BootloaderType.RPI_BOOT

    DEFAULT_RPI_MODEL_FPATH: ClassVar = "/proc/device-tree/model"
    RPI_MODEL_HINT: ClassVar = "Raspberry Pi 4 Model B"

    SLOT_A_FSLABEL: ClassVar = "slot_a"
    SLOT_B_FSLABEL: ClassVar = "slot_b"

    SYSTEM_BOOT_FSLABEL: ClassVar = "system-boot"
    SWITCH_BOOT_FLAG_FNAME: ClassVar = "._ota_switch_boot_finalized"

    # boot files fname
    CONFIG_TXT_FNAME: ClassVar = "config.txt"  # primary boot cfg
    TRYBOOT_TXT_FNAME: ClassVar = "tryboot.txt"  # tryboot boot cfg
    VMLINUZ_FNAME: ClassVar = "vmlinuz"
    INITRD_IMG_FNAME: ClassVar = "initrd.img"
    CMDLINE_TXT_FNAME: ClassVar = "cmdline.txt"

    @cached_computed_field
    def RPI_MODEL_FPATH(self) -> str:
        """The dynamically rooted location of rpi model query API.

        Default: /proc/device-tree/model
        """
        return replace_root(
            self.DEFAULT_RPI_MODEL_FPATH,
            cfg.DEFAULT_ACTIVE_ROOTFS,
            cfg.ACTIVE_ROOTFS,
        )

    @cached_computed_field
    def SYSTEM_BOOT_MOUNT_POINT(self) -> str:
        """The dynamically rooted location of rpi system-boot partition mount point.

        Default: /boot/firmware
        """
        return os.path.join(cfg.BOOT_DPATH, "firmware")

    @cached_computed_field
    def SWITCH_BOOT_FLAG_FPATH(self) -> str:
        """The dynamically rooted location of rpi switch boot flag file.

        Default: /boot/firmware/._ota_switch_boot_finalized
        """
        return os.path.join(self.SYSTEM_BOOT_MOUNT_POINT, self.SWITCH_BOOT_FLAG_FNAME)


grub_cfg = GrubControlConfig()
cboot_cfg = CBootControlConfig()
rpi_boot_cfg = RPIBootControlConfig()
