import platform
from dataclasses import dataclass, field
from logging import INFO


# fmt: off
@dataclass
class OtaClientServerConfig:
    SERVER_PORT: str = "50051"
    PRE_UPDATE_TIMEOUT: float = "300" # 5mins
    WAITING_SUBECU_ACK_UPDATE_REQ_TIMEOUT: float = "300" # 5mins
    WAITING_GET_SUBECU_STATUS: float = "300" # 5mins
    WAITING_SUBECU_READY_TIMEOUT: float = "3600" # 1h
    LOCAL_OTA_UPDATE_TIMEOUT: float = "3600" # 1h
    QUERYING_SUBECU_STATUS_TIMEOUT: float = "120" # 2mins
    LOOP_QUERYING_SUBECU_STATUS_INTERVAL: float = "6"

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
    PASSWD_FILE: str = "/etc/passwd"
    GROUP_FILE: str = "/etc/group"
    BOOT_OTA_PARTITION_FILE: str = "ota-partition"
    OTA_STATUS_FNAME: str = "status"
    OTA_VERSION_FNAME: str = "version"
    LOG_FORMAT: str = (
        "[%(asctime)s][%(levelname)s]-%(filename)s:%(funcName)s:%(lineno)d,%(message)s"
    )
    MOUNT_POINT: str = "/mnt/standby"


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
    SEPERATE_BOOT_MOUNT_POINT: str = "/mnt/standby_boot"
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
