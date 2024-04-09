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
"""ECU metadatas definition and parsing logic."""


from __future__ import annotations
import logging
import warnings
from enum import Enum
from pathlib import Path
from typing import List
from typing_extensions import Annotated

import yaml
from pydantic import AfterValidator, BeforeValidator, Field, IPvAnyAddress

from otaclient._utils.typing import StrOrPath, gen_strenum_validator, NetworkPort
from otaclient.configs._common import BaseFixedConfig

logger = logging.getLogger(__name__)


class BootloaderType(str, Enum):
    """Bootloaders that supported by otaclient.

    auto_detect: (DEPRECATED) at runtime detecting what bootloader is in use in this ECU.
    grub: generic x86_64 platform with grub.
    cboot: ADLink rqx-580, rqx-58g, with BSP 32.5.x.
        (theoretically other Nvidia jetson xavier devices using cboot are also supported)
    rpi_boot: raspberry pi 4 with eeprom version newer than 2020-10-28(with tryboot support).
    """

    AUTO_DETECT = "auto_detect"
    GRUB = "grub"
    CBOOT = "cboot"
    RPI_BOOT = "rpi_boot"

    @staticmethod
    def deprecation_validator(value: BootloaderType) -> BootloaderType:
        if value == BootloaderType.AUTO_DETECT:
            _warning_msg = (
                "bootloader field in ecu_info.yaml is not set or set to auto_detect(DEPRECATED), "
                "runtime bootloader type detection is UNRELIABLE and bootloader field SHOULD be "
                "set in ecu_info.yaml"
            )
            warnings.warn(_warning_msg, DeprecationWarning, stacklevel=2)
            logger.warning(_warning_msg)
        return value


class ECUContact(BaseFixedConfig):
    ecu_id: str
    ip_addr: IPvAnyAddress
    # NOTE(20240327): set the default as literal for now, in the future
    #   this will be service_cfg.CLIENT_CALL_PORT.
    port: NetworkPort = 50051


class ECUInfo(BaseFixedConfig):
    """ECU info configuration.

    Attributes:
        format_version: the ecu_info.yaml scheme version, current is 1.
        ecu_id: the unique ID of this ECU.
        ip_addr: the IP address OTA servicer listening on, default is <service_config.DEFAULT_SERVER_ADDRESS>.
        bootloader: the bootloader type of this ECU.
        available_ecu_ids: a list of ECU IDs that should be involved in OTA campaign.
        secondaries: a list of ECUContact objects for sub ECUs.
    """

    format_version: int = 1
    ecu_id: str
    # NOTE(20240327): set the default as literal for now, in the future
    #   when app_configs are fully backported this will be replaced by
    #   service_cfg.DEFAULT_SERVER_ADDRESS
    ip_addr: IPvAnyAddress = Field(default="127.0.0.1")
    bootloader: Annotated[
        BootloaderType,
        BeforeValidator(gen_strenum_validator(BootloaderType)),
        AfterValidator(BootloaderType.deprecation_validator),
    ] = BootloaderType.AUTO_DETECT
    available_ecu_ids: List[str] = Field(default_factory=list)
    secondaries: List[ECUContact] = Field(default_factory=list)

    def get_available_ecu_ids(self) -> list[str]:
        """
        NOTE: this method should be used instead of directly accessing the
              available_ecu_ids attrs for backward compatibility reason.
        """
        # onetime fix, if no availabe_ecu_id is specified,
        # add my_ecu_id into the list
        if len(self.available_ecu_ids) == 0:
            return [self.ecu_id]
        return self.available_ecu_ids.copy()


# NOTE: this is backward compatibility for old x1 that doesn't have
#       ecu_info.yaml installed.
DEFAULT_ECU_INFO = ECUInfo(
    format_version=1,  # current version is 1
    ecu_id="autoware",  # should be unique for each ECU in vehicle
)


def parse_ecu_info(ecu_info_file: StrOrPath) -> ECUInfo:
    try:
        _raw_yaml_str = Path(ecu_info_file).read_text()
    except FileNotFoundError as e:
        logger.warning(f"{ecu_info_file=} not found: {e!r}")
        logger.warning(f"use default ecu_info: {DEFAULT_ECU_INFO}")
        return DEFAULT_ECU_INFO

    try:
        loaded_ecu_info = yaml.safe_load(_raw_yaml_str)
        assert isinstance(loaded_ecu_info, dict), "not a valid yaml file"
        return ECUInfo.model_validate(loaded_ecu_info, strict=True)
    except Exception as e:
        logger.warning(f"{ecu_info_file=} is invalid: {e!r}\n{_raw_yaml_str=}")
        logger.warning(f"use default ecu_info: {DEFAULT_ECU_INFO}")
        return DEFAULT_ECU_INFO


# NOTE(20240327): set the default as literal for now,
#   in the future this will be app_cfg.ECU_INFO_FPATH
ecu_info = parse_ecu_info(ecu_info_file="/boot/ota/ecu_info.yaml")
