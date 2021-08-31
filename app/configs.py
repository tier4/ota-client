from pathlib import Path
from collections import UserDict
from typing import Any, Mapping

from logging import DEBUG, INFO, ERROR

__all__ = ('get_empty_conf', 'get_default_cfg', 'get_default_conf_dict', 'LOG_LEVEL_TABLE', 'DEFAULT_LOG_LEVEL')

#
# configs
#
LOG_LEVEL_TABLE = {}
DEFAULT_LOG_LEVEL = INFO
# LOG_LEVEL_TABLE = {
#     "bank": DEBUG,
#     "grub_control": DEBUG,
#     "ota_boot": DEBUG,
#     "ota_client": DEBUG,
#     "ota_client_service": DEBUG,
#     "ota_metadata": DEBUG,
#     "ota_status": DEBUG,
# }


class _DefaultConfiguration:
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
    GRUB_CFG_FILE = GRUB_DIR / "grub.cfg"
    GRUB_DEFAUT_FILE = ETC_DIR / "default/grub"
    FSTAB_FILE = ETC_DIR / "fstab"

    @classmethod
    def create_default_configuration_dict(cls) -> dict:
        return {
            k: v
            for k, v in cls.__dict__.items()
            if not k.startswith("__") and isinstance(v, Path)
        }

_DEFAULT_CONF_DICT = _DefaultConfiguration.create_default_configuration_dict()

def get_default_conf_dict():
    return _DEFAULT_CONF_DICT.copy()

class Configuration():
    def __init__(self, **kwargs) -> None:
        if kwargs:
            for k, v in kwargs.items():
                self.__setattr__(k, v)

def get_default_conf():
    return Configuration(**_DEFAULT_CONF_DICT)

def get_empty_conf():
    return Configuration()