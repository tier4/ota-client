from typing import Type
from app.boot_control.common import BootControllerProtocol
from app.configs import BOOT_LOADER


def get_boot_controller(bootloader: str) -> Type[BootControllerProtocol]:
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


BootController: Type[BootControllerProtocol] = get_boot_controller(BOOT_LOADER)

__all__ = ("BootController",)
