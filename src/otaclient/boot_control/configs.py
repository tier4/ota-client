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

from otaclient.configs import BootloaderType


class GrubControlConfig:
    """x86-64 platform, with grub as bootloader."""

    BOOTLOADER = BootloaderType.GRUB
    FSTAB_FILE_PATH = "/etc/fstab"
    GRUB_DIR = "/boot/grub"
    GRUB_CFG_FNAME = "grub.cfg"
    GRUB_CFG_PATH = "/boot/grub/grub.cfg"
    DEFAULT_GRUB_PATH = "/etc/default/grub"
    BOOT_OTA_PARTITION_FILE = "ota-partition"


class JetsonBootCommon:
    # ota_status related
    OTA_STATUS_DIR = "/boot/ota-status"
    FIRMWARE_BSP_VERSION_FNAME = "firmware_bsp_version"

    # boot control related
    EXTLINUX_FILE = "/boot/extlinux/extlinux.conf"
    MODEL_FPATH = "/proc/device-tree/model"
    NV_TEGRA_RELEASE_FPATH = "/etc/nv_tegra_release"
    SEPARATE_BOOT_MOUNT_POINT = "/mnt/standby_boot"

    # boot device related
    MMCBLK_DEV_PREFIX = "mmcblk"  # internal emmc
    NVMESSD_DEV_PREFIX = "nvme"  # external nvme ssd
    SDX_DEV_PREFIX = "sd"  # non-specific device name
    INTERNAL_EMMC_DEVNAME = "mmcblk0"

    # firmware update related
    NVBOOTCTRL_CONF_FPATH = "/etc/nv_boot_control.conf"
    FIRMWARE_DPATH = "/opt/ota/firmware"
    FIRMWARE_UPDATE_REQUEST_FPATH = f"{FIRMWARE_DPATH}/firmware_update.yaml"
    FIRMWARE_MANIFEST_FPATH = f"{FIRMWARE_DPATH}/firmware_manifest.yaml"


class JetsonCBootControlConfig(JetsonBootCommon):
    """Jetson device booted with cboot.

    Suuports BSP version < R34.
    """

    BOOTLOADER = BootloaderType.JETSON_CBOOT
    # this path only exists on xavier
    TEGRA_CHIP_ID_PATH = "/sys/module/tegra_fuse/parameters/tegra_chip_id"
    FIRMWARE_LIST = ["bl_only_payload", "xusb_only_payload"]


class JetsonUEFIBootControlConfig(JetsonBootCommon):
    BOOTLOADER = BootloaderType.JETSON_UEFI
    TEGRA_COMPAT_PATH = "/sys/firmware/devicetree/base/compatible"
    L4TLAUNCHER_FNAME = "BOOTAA64.efi"
    ESP_MOUNTPOINT = "/mnt/esp"
    ESP_PARTLABEL = "esp"
    UPDATE_TRIGGER_EFIVAR = "OsIndications-8be4df61-93ca-11d2-aa0d-00e098032b8c"
    MAGIC_BYTES = b"\x07\x00\x00\x00\x04\x00\x00\x00\x00\x00\x00\x00"
    CAPSULE_PAYLOAD_AT_ESP = "EFI/UpdateCapsule"
    L4TLAUNCHER_VER_FNAME = "l4tlauncher_version"


class RPIBootControlConfig:
    BBOOTLOADER = BootloaderType.RPI_BOOT
    RPI_MODEL_FILE = "/proc/device-tree/model"
    RPI_MODEL_HINT = "Raspberry Pi 4 Model B"

    # boot folders
    SYSTEM_BOOT_MOUNT_POINT = "/boot/firmware"
    OTA_STATUS_DIR = "/boot/ota-status"
    SWITCH_BOOT_FLAG_FILE = "._ota_switch_boot_finalized"


grub_cfg = GrubControlConfig()

jetson_common_cfg = JetsonBootCommon()
cboot_cfg = JetsonCBootControlConfig()
jetson_uefi_cfg = JetsonUEFIBootControlConfig()

rpi_boot_cfg = RPIBootControlConfig()
