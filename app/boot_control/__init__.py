from typing import Type
from app.boot_control.common import BootControllerProtocol
from app.boot_control.common import (
    BootControlError,
    BootControlInternalError,
    BootControlExternalError,
)
from app.configs import config as cfg

_bootloader = cfg.BOOTLOADER


def get_boot_controller(bootloader: str) -> Type[BootControllerProtocol]:
    if bootloader == "grub":
        from app.boot_control.grub import GrubController

        return GrubController

    elif bootloader == "cboot":
        from app.boot_control.cboot import CBootController

        return CBootController
    else:
        raise NotImplementedError(
            f"boot controller is not implemented for {_bootloader=}"
        )


BootController: Type[BootControllerProtocol] = get_boot_controller(_bootloader)

__all__ = (
    "BootController",
    "BootControlError",
    "BootControlInternalError",
    "BootControlExternalError",
)
