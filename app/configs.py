import enum
from dataclasses import dataclass, field
from logging import INFO
from typing import Literal


class OTAFileCacheControl(enum.Enum):
    """Custom header for ota file caching control policies.

    format:
        Ota-File-Cache-Control: <directive>
    directives:
        retry_cache: indicates that ota_proxy should clear cache entry for <URL>
            and retry caching
        no_cache: indicates that ota_proxy should not use cache for <URL>
        use_cache: implicitly applied default value, conflicts with no_cache directive
            no need(and no effect) to add this directive into the list

    NOTE: using retry_cache and no_cache together will not work as expected,
        only no_cache will be respected, already cached file will not be deleted as retry_cache indicates.
    """

    use_cache = "use_cache"
    no_cache = "no_cache"
    retry_caching = "retry_caching"

    header = "Ota-File-Cache-Control"
    header_lower = "ota-file-cache-control"

    @classmethod
    def parse_to_value_set(cls, input: str) -> "set[str]":
        return set(input.split(","))

    @classmethod
    def parse_to_enum_set(cls, input: str) -> "set[OTAFileCacheControl]":
        _policies_set = cls.parse_to_value_set(input)
        res = set()
        for p in _policies_set:
            res.add(OTAFileCacheControl[p])

        return res

    @classmethod
    def add_to(cls, target: str, input: "OTAFileCacheControl") -> str:
        _policies_set = cls.parse_to_value_set(target)
        _policies_set.add(input.value)
        return ",".join(_policies_set)


# fmt: off
@dataclass(frozen=True)
class OtaClientServerConfig:
    SERVER_PORT: str = "50051"
    PRE_UPDATE_TIMEOUT: float = 300  # 5mins
    WAITING_SUBECU_ACK_UPDATE_REQ_TIMEOUT: float = 300  # 5mins
    WAITING_GET_SUBECU_STATUS: float = 300  # 5mins
    WAITING_SUBECU_READY_TIMEOUT: float = 3600  # 1h
    QUERYING_SUBECU_STATUS_TIMEOUT: float = 120  # 2mins
    LOOP_QUERYING_SUBECU_STATUS_INTERVAL: float = 10

    # proxy server
    OTA_PROXY_LISTEN_ADDRESS: str = "0.0.0.0"
    OTA_PROXY_LISTEN_PORT: int = 8082


@dataclass
class _BaseConfig:
    DEFAULT_LOG_LEVEL: int = INFO
    LOG_LEVEL_TABLE: dict = field(
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
    BOOT_DIR: str = "/boot"
    ECU_INFO_FILE: str = "/boot/ota/ecu_info.yaml"
    PROXY_INFO_FILE: str = "/boot/ota/proxy_info.yaml"
    PASSWD_FILE: str = "/etc/passwd"
    GROUP_FILE: str = "/etc/group"
    BOOT_OTA_PARTITION_FILE: str = "ota-partition"
    OTA_STATUS_FNAME: str = "status"
    OTA_VERSION_FNAME: str = "version"
    LOG_FORMAT: str = (
        "[%(asctime)s][%(levelname)s]-%(filename)s:%(funcName)s:%(lineno)d,%(message)s"
    )
    MOUNT_POINT: str = "/mnt/standby"

    # ota-client behavior setting
    CHUNK_SIZE: int = 1 * 1024 * 1024  # 1MB
    DOWNLOAD_RETRY: int = 5
    DOWNLOAD_BACKOFF_MAX: int = 10 # seconds
    MAX_CONCURRENT_DOWNLOAD: int = 8
    MAX_CONCURRENT_TASKS: int = 1024
    STATS_COLLECT_INTERVAL: int = 1 # second

    ## standby creation mode
    STANDBY_CREATION_MODE: Literal["legacy", "rebuild", "in-place", "auto"] = "auto"


@dataclass
class GrubControlConfig(_BaseConfig):
    """
    x86-64 platform, using grub
    """

    BOOTLOADER: str = "grub"
    FSTAB_FILE: str = "/etc/fstab"
    GRUB_DIR: str = "/boot/grub"
    GRUB_CFG_FILE: str = "/boot/grub/grub.cfg"
    CUSTOM_CFG_FILE: str = "/boot/grub/custom.cfg"
    DEFAULT_GRUB_FILE: str = "/etc/default/grub"


@dataclass
class CBootControlConfig(_BaseConfig):
    """
    NOTE: only for tegraid:0x19, roscube-x platform(jetson-xavier-agx series)
    """

    BOOTLOADER: str = "cboot"
    CHIP_ID_MODEL_MAP: dict = field(default_factory=lambda: {0x19: "rqx_580"})
    
    # ota related status files are stored under OTA_STATUS_DIR
    # include: status, slot_in_use, version
    OTA_STATUS_DIR: str = "/boot/ota-status"
    SLOT_IN_USE_FNAME: str = "slot_in_use"

    # mount point
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
        raise NotImplementedError(f"cannot auto detect the bootloader for this platform: {machine=}, {arch=}")


def create_config(bootloader: str):
    if bootloader == "grub":
        return GrubControlConfig()
    elif bootloader == "cboot":
        return CBootControlConfig()
    else:
        raise NotImplementedError(f"{bootloader=} not supported, abort")


config = create_config(_detect_bootloader())
server_cfg = OtaClientServerConfig()
# fmt: on
