import enum
import platform
from dataclasses import dataclass, field
from logging import INFO

from typing import Tuple


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
    OTA_PROXY_SERVER_ADDR: Tuple[str, int] = ("0.0.0.0", 8082)


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

    # ota-update download setting
    MAX_CONCURRENT_DOWNLOAD: int = 16
    MAX_CONCURRENT_TASKS: int = 128


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
    EXTLINUX_FILE: str = "/boot/extlinux/extlinux.conf"
    SLOT_IN_USE_FILE: str = "/boot/ota-status/slot_in_use"
    OTA_STATUS_DIR: str = "/boot/ota-status"
    KERNEL: str = "/boot/Image"
    KERNEL_SIG: str = "/boot/Image.sig"
    INITRD: str = "/boot/initrd"
    INITRD_IMG_LINK: str = "/boot/initrd.img"
    FDT: str = "/boot/tegra194-rqx-580.dtb"
    FDT_HDR40: str = "/boot/tegra194-rqx-580-hdr40.dtbo"
    SEPARATE_BOOT_MOUNT_POINT: str = "/mnt/standby_boot"
    EXTRA_CMDLINE: str = (
        "console=ttyTCU0,115200n8 console=tty0 fbcon=map:0 net.ifnames=0"
    )


# helper function to detect platform
def _detect_bootloader():
    if platform.machine() == "x86_64" or platform.processor == "x86_64":
        return "grub"
    elif platform.machine() == "aarch64" or platform.processor == "aarch64":
        return "cboot"
    else:
        return ""


def create_config(bootloader):
    if bootloader == "grub":
        return GrubControlConfig()
    elif bootloader == "cboot":
        return CBootControlConfig()
    else:
        raise NotImplementedError(f"{bootloader=} not supported, abort")


config = create_config(_detect_bootloader())
server_cfg = OtaClientServerConfig()
# fmt: on
