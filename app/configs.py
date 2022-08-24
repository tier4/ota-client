from dataclasses import dataclass, field
from enum import Enum, auto
from logging import INFO
from typing import Dict


class CreateStandbyMechanism(Enum):
    LEGACY = 0
    REBUILD = auto()
    IN_PLACE = auto()


@dataclass(frozen=True)
class OtaClientServerConfig:
    SERVER_PORT: int = 50051
    WAITING_SUBECU_ACK_UPDATE_REQ_TIMEOUT: float = 6
    QUERYING_SUBECU_STATUS_TIMEOUT: float = 30
    LOOP_QUERYING_SUBECU_STATUS_INTERVAL: float = 10
    STATUS_UPDATE_INTERVAL: float = 1

    # proxy server
    OTA_PROXY_LISTEN_ADDRESS: str = "0.0.0.0"
    OTA_PROXY_LISTEN_PORT: int = 8082


@dataclass
class BaseConfig:
    """Platform neutral configuration."""

    DEFAULT_LOG_LEVEL: int = INFO
    LOG_LEVEL_TABLE: Dict[str, int] = field(
        default_factory=lambda: {
            "ecu_info": INFO,
            "grub_control": INFO,
            "grub_ota_partition": INFO,
            "extlinux_control": INFO,
            "main": INFO,
            "ota_client": INFO,
            "ota_client_call": INFO,
            "ota_client_service": INFO,
            "ota_client_stub": INFO,
            "ota_metadata": INFO,
            "ota_status": INFO,
        }
    )
    ACTIVE_ROOTFS_PATH: str = "/"
    BOOT_DIR: str = "/boot"
    ECU_INFO_FILE: str = "/boot/ota/ecu_info.yaml"
    PROXY_INFO_FILE: str = "/boot/ota/proxy_info.yaml"
    PASSWD_FILE: str = "/etc/passwd"
    GROUP_FILE: str = "/etc/group"

    # status files
    OTA_STATUS_FNAME: str = "status"
    OTA_VERSION_FNAME: str = "version"
    SLOT_IN_USE_FNAME: str = "slot_in_use"

    LOG_FORMAT: str = (
        "[%(asctime)s][%(levelname)s]-%(filename)s:%(funcName)s:%(lineno)d,%(message)s"
    )

    # standby/refroot mount points
    MOUNT_POINT: str = "/mnt/standby"
    # where active(old) image partition will be bind mounted to
    REF_ROOT_MOUNT_POINT: str = "/mnt/refroot"

    # ota-client behavior setting
    CHUNK_SIZE: int = 1 * 1024 * 1024  # 1MB
    LOCAL_CHUNK_SIZE: int = 4 * 1024 * 1024  # 4MB
    DOWNLOAD_RETRY: int = 5
    DOWNLOAD_BACKOFF_MAX: int = 3  # seconds
    MAX_CONCURRENT_DOWNLOAD: int = 8
    MAX_CONCURRENT_TASKS: int = 128
    STATS_COLLECT_INTERVAL: int = 1  # second
    ## standby creation mode, default to rebuild now
    STANDBY_CREATION_MODE = CreateStandbyMechanism.REBUILD
    # NOTE: the following 2 folders are meant to be located under standby_slot
    OTA_TMP_STORE: str = "/ota-tmp"
    META_FOLDER: str = "/opt/ota/image-meta"


@dataclass
class GrubControlConfig(BaseConfig):
    """x86-64 platform, with grub as bootloader."""

    BOOTLOADER: str = "grub"
    FSTAB_FILE_PATH: str = "/etc/fstab"
    GRUB_DIR: str = "/boot/grub"
    GRUB_CFG_PATH: str = "/boot/grub/grub.cfg"
    CUSTOM_CFG_PATH: str = "/boot/grub/custom.cfg"
    DEFAULT_GRUB_PATH: str = "/etc/default/grub"
    BOOT_OTA_PARTITION_FILE: str = "ota-partition"


@dataclass
class CBootControlConfig(BaseConfig):
    """arm platform, with cboot as bootloader.

    NOTE: only for tegraid:0x19, roscube-x platform(jetson-xavier-agx series)
    """

    BOOTLOADER: str = "cboot"
    CHIP_ID_MODEL_MAP: Dict[int, str] = field(default_factory=lambda: {0x19: "rqx_580"})
    OTA_STATUS_DIR: str = "/boot/ota-status"
    EXTLINUX_FILE: str = "/boot/extlinux/extlinux.conf"
    SEPARATE_BOOT_MOUNT_POINT: str = "/mnt/standby_boot"


# helper function to detect platform
def _detect_bootloader():
    import platform

    machine, arch = platform.machine(), platform.processor()

    if machine == "x86_64" or arch == "x86_64":
        return "grub"
    elif machine == "aarch64" or arch == "aarch64":
        return "cboot"
    else:
        raise NotImplementedError(
            f"cannot auto detect the bootloader for this platform: "
            f"{machine=}, {arch=}"
        )


BOOT_LOADER = _detect_bootloader()

# init cfgs
server_cfg = OtaClientServerConfig()
cboot_cfg = CBootControlConfig()
grub_cfg = GrubControlConfig()
config = BaseConfig()
