from typing import Type
from .interface import BootControllerProtocol

from ..configs import BOOT_LOADER, config as cfg
from .. import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


def get_boot_controller(bootloader: str = BOOT_LOADER) -> Type[BootControllerProtocol]:
    logger.info(f"use boot_controller {bootloader=}")
    if bootloader == "grub":
        from .grub import GrubController

        return GrubController

    elif bootloader == "cboot":
        from .cboot import CBootController

        return CBootController
    else:
        raise NotImplementedError(
            f"boot controller is not implemented for {bootloader=}"
        )


__all__ = ("get_boot_controller",)
