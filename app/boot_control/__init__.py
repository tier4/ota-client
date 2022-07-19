from typing import Type
from app.boot_control.interface import BootControllerProtocol

from app.configs import BOOT_LOADER, config as cfg
from app import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


def get_boot_controller(bootloader: str = BOOT_LOADER) -> Type[BootControllerProtocol]:
    logger.info(f"use boot_controller {bootloader=}")
    if bootloader == "grub":
        from app.boot_control.grub import GrubController

        return GrubController

    elif bootloader == "cboot":
        from app.boot_control.cboot import CBootController

        return CBootController
    else:
        raise NotImplementedError(
            f"boot controller is not implemented for {bootloader=}"
        )


__all__ = ("get_boot_controller",)
