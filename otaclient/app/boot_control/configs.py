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

from dataclasses import dataclass, field
from typing import Dict, List

from otaclient.configs.ecu_info import BootloaderType

from ..configs import BaseConfig


@dataclass
class GrubControlConfig(BaseConfig):
    """x86-64 platform, with grub as bootloader."""

    BOOTLOADER: BootloaderType = BootloaderType.GRUB
    FSTAB_FILE_PATH: str = "/etc/fstab"
    GRUB_DIR: str = "/boot/grub"
    GRUB_CFG_FNAME: str = "grub.cfg"
    GRUB_CFG_PATH: str = "/boot/grub/grub.cfg"
    DEFAULT_GRUB_PATH: str = "/etc/default/grub"
    BOOT_OTA_PARTITION_FILE: str = "ota-partition"


@dataclass
class JetsonCBootControlConfig(BaseConfig):
    """Jetson device booted with cboot.

    Suuports BSP version < R34.
    """

    BOOTLOADER: BootloaderType = BootloaderType.CBOOT
    TEGRA_CHIP_ID_PATH: str = "/sys/module/tegra_fuse/parameters/tegra_chip_id"
    CHIP_ID_MODEL_MAP: Dict[int, str] = field(default_factory=lambda: {0x19: "rqx_580"})
    OTA_STATUS_DIR: str = "/boot/ota-status"
    FIRMWARE_BSP_VERSION_FNAME: str = "firmware_bsp_version"
    EXTLINUX_FILE: str = "/boot/extlinux/extlinux.conf"
    SEPARATE_BOOT_MOUNT_POINT: str = "/mnt/standby_boot"
    # refer to the standby slot
    FIRMWARE_DPATH: str = "/opt/ota_package"
    FIRMWARE_LIST: List[str] = field(
        default_factory=lambda: ["bl_only_payload", "xusb_only_payload"]
    )
    NV_TEGRA_RELEASE_FPATH: str = "/etc/nv_tegra_release"


@dataclass
class RPIBootControlConfig(BaseConfig):
    BBOOTLOADER: BootloaderType = BootloaderType.RPI_BOOT
    RPI_MODEL_FILE = "/proc/device-tree/model"
    RPI_MODEL_HINT = "Raspberry Pi 4 Model B"

    # slot configuration
    SLOT_A_FSLABEL = "slot_a"
    SLOT_B_FSLABEL = "slot_b"
    SYSTEM_BOOT_FSLABEL = "system-boot"

    # boot folders
    SYSTEM_BOOT_MOUNT_POINT = "/boot/firmware"
    OTA_STATUS_DIR = "/boot/ota-status"

    # boot related files
    CONFIG_TXT = "config.txt"  # primary boot cfg
    TRYBOOT_TXT = "tryboot.txt"  # tryboot boot cfg
    VMLINUZ = "vmlinuz"
    INITRD_IMG = "initrd.img"
    CMDLINE_TXT = "cmdline.txt"
    SWITCH_BOOT_FLAG_FILE = "._ota_switch_boot_finalized"


grub_cfg = GrubControlConfig()
cboot_cfg = JetsonCBootControlConfig()
rpi_boot_cfg = RPIBootControlConfig()
