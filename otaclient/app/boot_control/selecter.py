# Copyright 2022 TIER IV, INC. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import platform
from pathlib import Path
from typing import Type

from .configs import BootloaderType, cboot_cfg, rpi_boot_cfg
from ._errors import BootControlError
from .protocol import BootControllerProtocol

from ..configs import config as cfg
from ..common import read_str_from_file
from .. import log_setting

logger = log_setting.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


def detect_bootloader(raise_on_unknown=True) -> BootloaderType:
    """Detect which bootloader we are running with by some evidence.

    Evidence:
        1. assuming all x86_64 platform devices are using grub,
        2. for aarch64, check TEGRA_CHIP_ID_PATH for jetson xavier device,
        3. for aarch64, check RPI_MODEL_FILE with RPI_MODEL_HINT for rpi device.

    NOTE: this function is only used when ecu_info.yaml doesn't
          contains the bootloader information.
    """
    # evidence: assuming that all x86 device is using grub
    machine, arch = platform.machine(), platform.processor()
    if machine == "x86_64" or arch == "x86_64":
        return BootloaderType.GRUB
    # distinguish between rpi and jetson xavier device
    if machine == "aarch64" or arch == "aarch64":
        # evidence: jetson xvaier device has a special file which reveals the
        #           tegra chip id
        if Path(cboot_cfg.TEGRA_CHIP_ID_PATH).is_file():
            return BootloaderType.CBOOT
        # evidence: rpi device has a special file which reveals the rpi model
        rpi_model_file = Path(rpi_boot_cfg.RPI_MODEL_FILE)
        if rpi_model_file.is_file():
            if (_model_str := read_str_from_file(rpi_model_file)).find(
                rpi_boot_cfg.RPI_MODEL_HINT
            ) != -1:
                return BootloaderType.RPI_BOOT
            else:
                logger.error(
                    f"detect unsupported raspberry pi platform({_model_str=}), only {rpi_boot_cfg.RPI_MODEL_HINT} is supported"
                )

    # failed to detect bootloader
    if raise_on_unknown:
        raise ValueError(f"unsupported platform({machine=}, {arch=}) detected, abort")
    return BootloaderType.UNSPECIFIED


def get_boot_controller(
    bootloader_type: BootloaderType,
) -> Type[BootControllerProtocol]:
    # if ecu_info doesn't specify the bootloader type,
    # we try to detect by ourselves
    if bootloader_type is BootloaderType.UNSPECIFIED:
        bootloader_type = detect_bootloader(raise_on_unknown=True)
    logger.info(f"use boot_controller for {bootloader_type=}")

    if bootloader_type == BootloaderType.GRUB:
        from ._grub import GrubController

        return GrubController
    if bootloader_type == BootloaderType.CBOOT:
        from ._cboot import CBootController

        return CBootController
    if bootloader_type == BootloaderType.RPI_BOOT:
        from ._rpi_boot import RPIBootController

        return RPIBootController

    raise BootControlError from NotImplementedError(f"unsupported: {bootloader_type=}")
