from pathlib import Path

import bank
import grub_control
import ota_boot
import ota_client
import ota_client_service
import ota_status
import ota_metadata

from logging import DEBUG, INFO, ERROR

_configs_dir_list = (
    "OTA_DIR",
    "GRUB_DIR",
    "ETC_DIR",
    "MOUNT_POINT",
    "OTA_CACHE_DIR",
    "TMP_DIR",
)
_configs_file_list = (
    "OTA_STATUS_FILE",
    "BANK_INFO_FILE",
    "ECUID_FILE",
    "ECUINFO_YAML_FILE",
    "CUSTOM_CONFIG_FILE",
    "GRUB_CFG_FILE",
    "GRUB_DEFAUT_FILE",
    "FSTAB_FILE",
    "OTA_ROLLBACK_FILE",
    "OTA_METADATA_FILE",
)

__all__ = _configs_dir_list + _configs_file_list
# __all__ = ('GRUB_DEFAULT_FILE')

# configs
# LOG_LEVEL_TABLE = {}
LOG_LEVEL_TABLE = {
    bank.__name__: DEBUG,
    grub_control.__name__: DEBUG,
    ota_boot.__name__: DEBUG,
    ota_client.__name__: DEBUG,
    ota_client_service.__name__: DEBUG,
    ota_metadata.__name__: DEBUG,
    ota_status.__name__: DEBUG,
}

#
# dirs
#
OTA_DIR = Path("/boot/ota")
GRUB_DIR = Path("/boot/grub")
ETC_DIR = Path("/etc")
MOUNT_POINT = Path("/mnt/bank")
ROLLBACK_DIR = Path("/boot/ota/rollback")
OTA_CACHE_DIR = Path("/tmp/ota-cache")
TMP_DIR = Path("/tmp")

#
# files
#
# ota_client
OTA_STATUS_FILE = OTA_DIR / "ota_status"
BANK_INFO_FILE = OTA_DIR / "bankinfo.yaml"
ECUID_FILE = OTA_DIR / "ecuid"
ECUINFO_YAML_FILE = OTA_DIR / "ecuinfo.yaml"
CUSTOM_CONFIG_FILE = GRUB_DIR / "custom.cfg"
OTA_ROLLBACK_FILE = OTA_DIR / "ota_rollback_count"
OTA_METADATA_FILE = OTA_DIR / "metadata.jwt"
# system file
GRUB_CFG_FILE = GRUB_DIR / "grub.conf"
GRUB_DEFAUT_FILE = ETC_DIR / "default/grub"
FSTAB_FILE = ETC_DIR / "fstab"
