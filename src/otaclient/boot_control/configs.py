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

from dataclasses import dataclass

from otaclient.app.configs import BaseConfig
from otaclient.configs.ecu_info import BootloaderType


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


class JetsonBootCommon:
    TEGRA_CHIP_ID_PATH = "/sys/module/tegra_fuse/parameters/tegra_chip_id"
    OTA_STATUS_DIR = "/boot/ota-status"
    FIRMWARE_BSP_VERSION_FNAME = "firmware_bsp_version"
    EXTLINUX_FILE = "/boot/extlinux/extlinux.conf"
    FIRMWARE_DPATH = "/opt/ota_package"
    """Refer to standby slot rootfs."""

    NV_TEGRA_RELEASE_FPATH = "/etc/nv_tegra_release"
    SEPARATE_BOOT_MOUNT_POINT = "/mnt/standby_boot"

    MMCBLK_DEV_PREFIX = "mmcblk"  # internal emmc
    NVMESSD_DEV_PREFIX = "nvme"  # external nvme ssd
    INTERNAL_EMMC_DEVNAME = "mmcblk0"


class JetsonCBootControlConfig(JetsonBootCommon):
    """Jetson device booted with cboot.

    Suuports BSP version < R34.
    """

    BOOTLOADER = BootloaderType.JETSON_CBOOT
    FIRMWARE_LIST = ["bl_only_payload", "xusb_only_payload"]


@dataclass
class RPIBootControlConfig(BaseConfig):
    BBOOTLOADER: BootloaderType = BootloaderType.RPI_BOOT
    RPI_MODEL_FILE = "/proc/device-tree/model"
    RPI_MODEL_HINT = "Raspberry Pi 4 Model B"

    # boot folders
    SYSTEM_BOOT_MOUNT_POINT = "/boot/firmware"
    OTA_STATUS_DIR = "/boot/ota-status"
    SWITCH_BOOT_FLAG_FILE = "._ota_switch_boot_finalized"


grub_cfg = GrubControlConfig()
cboot_cfg = JetsonCBootControlConfig()
rpi_boot_cfg = RPIBootControlConfig()
