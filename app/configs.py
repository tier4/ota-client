from pathlib import Path

_configs_dir_list = (
    'OTA_DIR',
    'GRUB_DIR',
    'ETC_DIR',
    'MOUNT_POINT'
)
_configs_file_list = (
    'OTA_STATUS_FILE',
    'BANK_INFO_FILE',
    'ECUID_FILE',
    'ECUINFO_YAML_FILE',
    'CUSTOM_CONFIG_FILE',
    'GRUB_CFG_FILE',
    'GRUB_DEFAUT_FILE',
    'FSTAB_FILE'
)

__all__ = _configs_dir_list + _configs_file_list
# __all__ = ('GRUB_DEFAULT_FILE')

#
# dirs
#
OTA_DIR=Path("/boot/ota")
GRUB_DIR=Path("/boot/grub")
ETC_DIR=Path("/etc")
MOUNT_POINT=Path("/mnt/bank")
ROLLBACK_DIR=Path("/boot/ota/rollback")

#
# files
#
# ota_client
OTA_STATUS_FILE=OTA_DIR / "ota_status"
BANK_INFO_FILE=OTA_DIR / "bankinfo.yaml"
ECUID_FILE=OTA_DIR / "ecuid"
ECUINFO_YAML_FILE=OTA_DIR / "ecuinfo.yaml"
CUSTOM_CONFIG_FILE=GRUB_DIR / "custom.cfg"
# system file
GRUB_CFG_FILE=GRUB_DIR / "grub.conf"
GRUB_DEFAUT_FILE=ETC_DIR / "default/grub"
FSTAB_FILE=ETC_DIR / "fstab"