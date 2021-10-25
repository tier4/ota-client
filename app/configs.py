from pathlib import Path

from logging import INFO

#
# configs
#
DEFAULT_LOG_LEVEL = INFO
LOG_LEVEL_TABLE = {
    "ecu_info": INFO,
    "grub_control": INFO,
    "main": INFO,
    "ota_client": INFO,
    "ota_client_call": INFO,
    "ota_client_service": INFO,
    "ota_client_stub": INFO,
    "ota_metadata": INFO,
    "ota_partition": INFO,
    "ota_status": INFO,
}
LOG_FORMAT = "[%(asctime)s][%(levelname)s]-%(filename)s:%(lineno)d,%(message)s"

#
# dirs
#
BOOT_DIR = Path("/boot")
GRUB_DIR = BOOT_DIR / "grub"
ETC_DIR = Path("/etc")
MOUNT_POINT = Path("/mnt/standby")


#
# files
#
GRUB_CFG_FILE = GRUB_DIR / "grub.cfg"
CUSTOM_CFG_FILE = GRUB_DIR / "custom.cfg"
FSTAB_FILE = ETC_DIR / "fstab"
DEFAULT_GRUB_FILE = ETC_DIR / "default/grub"
ECU_INFO_FILE = BOOT_DIR / "ota" / "ecu_info.yaml"
PASSWD_FILE = ETC_DIR / "passwd"
GROUP_FILE = ETC_DIR / "group"

BOOT_OTA_PARTITION_FILE = Path("ota-partition")
