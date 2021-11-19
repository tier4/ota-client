import platform
from abc import ABC
from pathlib import Path

from logging import INFO


class _BaseConfig(ABC):

    # default settings(platform neutral)
    default_log_level = INFO
    log_level_table = {
        "ecu_info": INFO,
        "grub_control": INFO,
        "grub_ota_partition": INFO,
        "main": INFO,
        "ota_client": INFO,
        "ota_client_call": INFO,
        "ota_client_service": INFO,
        "ota_client_stub": INFO,
        "ota_metadata": INFO,
        "ota_status": INFO,
    }

    boot_dir = Path("/boot")
    etc_dir = Path("/etc")

    fstab_file = etc_dir / "fstab"
    ecu_info_file = boot_dir / "ota" / "ecu_info.yaml"
    passwd_file = etc_dir / "passwd"
    group_file = etc_dir / "group"

    ota_partition_folder = Path("ota-partition")

    # properties map
    _properties_map = {
        "DEFAULT_LOG_LEVEL": default_log_level,
        "LOG_LEVEL_TABLE": log_level_table,
        "BOOT_DIR": boot_dir,
        "ETC_DIR": etc_dir,
        "FSTAB_FILE": fstab_file,
        "ECU_INFO_FILE": ecu_info_file,
        "PASSWD_FILE": passwd_file,
        "GROUP_FILE": group_file,
        "BOOT_OTA_PARTITION_FILE": ota_partition_folder,
        "OTA_STATUS_FNAME": "status",
        "OTA_VERSION_FNAME": "version",
        "LOG_FORMAT": "[%(asctime)s][%(levelname)s]-%(filename)s:%(funcName)s:%(lineno)d,%(message)s",
    }

    def __getattr__(self, name: str):
        if name not in self._properties_map:
            raise AttributeError(f"config option {name} not found")
        else:
            return self._properties_map[name]


class GrubControlConfig(_BaseConfig):
    """
    x86-64 platform, using grub
    """

    PLATFORM = "grub"

    def __init__(self):
        self.grub_dir = self.boot_dir / "grub"
        self.grub_cfg_file = self.grub_dir / "grub.cfg"
        self.custom_cfg_file = self.grub_dir / "custom.cfg"
        self.default_grub_file = self.grub_dir / "default/grub"
        self.mount_point = Path("/mnt/standby")

        self._properties_map.update(
            {
                "GRUB_DIR": self.grub_dir,
                "GRUB_CFG_FILE": self.grub_cfg_file,
                "CUSTOM_CFG_FILE": self.custom_cfg_file,
                "DEFAULT_GRUB_FILE": self.default_grub_file,
                "MOUNT_POINT": self.mount_point,
            }
        )


class CBootControlConfig(_BaseConfig):
    """
    NOTE: only for tegraid:0x19, jetson xavier platform
    """

    PLATFORM = "cboot"

    def __init__(self):
        self.extlinux_file = self.boot_dir / "extlinux/extlinux.conf"

        self._properties_map.update(
            {
                "EXLINUX_FILE": self.extlinux_file,
                "OTA_STATUS_DIR": self.boot_dir / "ota-status",
                "LINUX": self.boot_dir / "Image",
                "INITRD": self.boot_dir / "initrd",
                "FDT": self.boot_dir / "tegra194-rqx-580.dtb",
                "EXTRA_CMDLINE": "console=ttyTCU0,115200n8 console=tty0 fbcon=map:0 net.ifnames=0",
            }
        )


# helper function to detect platform
def _detect_platform():
    if platform.machine() == "x86_64" or platform.processor == "x86_64":
        return "grub"
    elif platform.machine() == "aarch64" or platform.processor == "aarch64":
        return "cboot"

def create_config(platform):
    if platform == "grub":
        return GrubControlConfig()
    elif platform == "cboot":
        return CBootControlConfig()


cboot_cfg = CBootControlConfig()
grub_cfg = GrubControlConfig()

Config = create_config(_detect_platform())
