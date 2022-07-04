from typing import Type
from app.boot_control.interface import BootControllerProtocol
from app.configs import BOOT_LOADER


def get_boot_controller(bootloader: str = BOOT_LOADER) -> Type[BootControllerProtocol]:
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
