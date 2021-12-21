import platform
from dataclasses import dataclass, field
from pathlib import Path
from logging import INFO


@dataclass(frozen=True)
class _BaseConfig:
    DEFAULT_LOG_LEVEL: int = (INFO,)
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
        }.copy()
    )
    ECU_INFO_FILE: Path = (Path("/boot/ota/ecu_info.yaml"),)
    PASSWD_FILE: Path = (Path("/etc/passwd"),)
    GROUP_FILE: Path = (Path("/etc/group"),)
    BOOT_OTA_PARTITION_FILE: Path = (Path("/boot/ota-partition"),)
    OTA_STATUS_FNAME: str = ("status",)
    OTA_VERSION_FNAME: str = ("version",)
    LOG_FORMAT: str = (
        "[%(asctime)s][%(levelname)s]-%(filename)s:%(funcName)s:%(lineno)d,%(message)s",
    )
    MOUNT_POINT: Path = Path("/mnt/standby")


@dataclass(frozen=True)
class GrubControlConfig(_BaseConfig):
    """
    x86-64 platform, using grub
    """

    BOOTLOADER: str = "grub"
    FSTAB_FILE: Path = (Path("/etc/fstab"),)
    GRUB_DIR: Path = (Path("/boot/grub"),)
    GRUB_CFG_FILE: Path = (Path("/boot/grub/grub.cfg"),)
    CUSTOM_CFG_FILE: Path = (Path("/boot/grub/custom.cfg"),)
    DEFAULT_GRUB_FILE: Path = (Path("/etc/default/grub"),)


@dataclass(frozen=True)
class CBootControlConfig(_BaseConfig):
    """
    NOTE: only for tegraid:0x19, roscube-x platform(jetson-xavier-agx series)
    """

    BOOTLOADER: str = "cboot"
    CHIP_ID_MODEL_MAP: dict = field(default_factory=lambda: {0x19: "rqx_580"}.copy())
    EXTLINUX_FILE: Path = ("/boot/extlinux/extlinux.conf",)
    SLOT_IN_USE_FILE: Path = ("/boot/ota-status/slot_in_use",)
    OTA_STATUS_DIR: Path = ("/boot/ota-status",)
    KERNEL: Path = ("/boot/Image",)
    KERNEL_SIG: Path = ("/boot/Image.sig",)
    INITRD: Path = ("/boot/initrd",)
    INITRD_IMG_LINK: Path = ("/boot/initrd.img",)
    FDT: Path = ("/boot/tegra194-rqx-580.dtb",)
    FDT_HDR40: Path = ("/boot/tegra194-rqx-580-hdr40.dtbo",)
    SEPERATE_BOOT_MOUNT_POINT: Path = (Path("/mnt/standby_boot"),)
    EXTRA_CMDLINE: str = (
        "console=ttyTCU0,115200n8 console=tty0 fbcon=map:0 net.ifnames=0",
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