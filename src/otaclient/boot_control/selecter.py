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


from __future__ import annotations

import logging
import platform
from pathlib import Path

from typing_extensions import deprecated

from otaclient.boot_control.protocol import BootControllerProtocol
from otaclient.configs import BootloaderType
from otaclient_common._io import read_str_from_file

logger = logging.getLogger(__name__)


@deprecated("bootloader type SHOULD be set in ecu_info.yaml instead of detecting")
def detect_bootloader() -> BootloaderType:
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
        from .configs import cboot_cfg, rpi_boot_cfg

        # evidence: jetson xvaier device has a special file which reveals the
        #           tegra chip id
        if Path(cboot_cfg.TEGRA_CHIP_ID_PATH).is_file():
            return BootloaderType.CBOOT

        # evidence: rpi device has a special file which reveals the rpi model
        rpi_model_file = Path(rpi_boot_cfg.RPI_MODEL_FILE)
        if rpi_model_file.is_file():
            if (_model_str := read_str_from_file(rpi_model_file, _default="")).find(
                rpi_boot_cfg.RPI_MODEL_HINT
            ) != -1:
                return BootloaderType.RPI_BOOT

            logger.error(
                f"detect unsupported raspberry pi platform({_model_str=}), "
                f"only {rpi_boot_cfg.RPI_MODEL_HINT} is supported"
            )
    raise ValueError(f"unsupported platform({machine=}, {arch=}) detected, abort")


def get_boot_controller(
    bootloader_type: BootloaderType,
) -> type[BootControllerProtocol]:
    # if ecu_info doesn't specify the bootloader type,
    # we try to detect by ourselves.
    if bootloader_type is BootloaderType.AUTO_DETECT:
        bootloader_type = detect_bootloader()
    logger.info(f"use boot_controller for {bootloader_type=}")

    if bootloader_type == BootloaderType.GRUB:
        from ._grub import GrubController

        return GrubController
    if (
        bootloader_type == BootloaderType.CBOOT
        or bootloader_type == BootloaderType.JETSON_CBOOT
    ):
        from ._jetson_cboot import JetsonCBootControl

        return JetsonCBootControl

    if bootloader_type == BootloaderType.JETSON_UEFI:
        from ._jetson_uefi import JetsonUEFIBootControl

        return JetsonUEFIBootControl

    if bootloader_type == BootloaderType.RPI_BOOT:
        from ._rpi_boot import RPIBootController

        return RPIBootController

    raise NotImplementedError(f"unsupported: {bootloader_type=}")
