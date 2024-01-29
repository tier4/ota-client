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
r"""ECU metadatas definition.

Version 1 scheme example:
    format_vesrion: 1
    ecu_id: "autoware"
    ip_addr: "0.0.0.0"
    bootloader: "grub"
    secondaries:
        - ecu_id: "p1"
            ip_addr: "0.0.0.0"
    available_ecu_ids:
        - "autoware"
        - "p1
"""


from __future__ import annotations
import logging
import yaml
from enum import Enum
from pathlib import Path
from pydantic import BeforeValidator, Field
from typing import List
from typing_extensions import Annotated

from otaclient._utils.typing import StrOrPath, gen_strenum_validator
from otaclient.configs._common import (
    BaseFixedConfig,
    NetworkPort,
    IPAddressAny,
)
from otaclient.configs.app_cfg import app_config
from otaclient.configs.ota_service_cfg import service_config

logger = logging.getLogger(__name__)


class BootloaderType(str, Enum):
    """Bootloaders that supported by otaclient.

    grub: generic x86_64 platform with grub
    cboot: ADLink rqx-580, rqx-58g, with BSP 32.5.x
        (theoretically other Nvidia jetson xavier devices using cboot are also supported)
    rpi_boot: raspberry pi 4 with eeprom version newer than 2020-10-28
    """

    UNSPECIFIED = "unspecified"
    GRUB = "grub"
    CBOOT = "cboot"
    RPI_BOOT = "rpi_boot"


class ECUContact(BaseFixedConfig):
    ecu_id: str
    ip_addr: IPAddressAny
    port: NetworkPort = service_config.CLIENT_CALL_PORT


class ECUInfo(BaseFixedConfig):
    """ECU info configuration.

    Attributes:
        format_version: the ecu_info.yaml scheme version, current is 1.
        ecu_id: the unique ID of this ECU.
        ip_addr: the IP address OTA servicer listening on, default is <service_config.DEFAULT_SERVER_ADDRESS>.
        bootloader: the bootloader type of this ECU, default is UNSPECIFIC(detect at runtime).
        available_ecu_ids: a list of ECU IDs that should be involved in OTA campaign.
        secondaries: a list of ECUContact objects for sub ECUs.
    """

    format_version: int = 1
    ecu_id: str
    ip_addr: IPAddressAny = service_config.DEFAULT_SERVER_ADDRESS
    bootloader: Annotated[
        BootloaderType,
        BeforeValidator(gen_strenum_validator(BootloaderType)),
    ] = BootloaderType.UNSPECIFIED
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
        loaded_ecu_info = yaml.safe_load(_raw_yaml_str)
        assert isinstance(loaded_ecu_info, dict), "not a valid yaml file"
        return ECUInfo.model_validate(loaded_ecu_info, strict=True)
    except Exception as e:
        logger.warning(f"{ecu_info_file=} is missing or invalid: {e!r}")
        logger.warning(f"use default ecu_info: {DEFAULT_ECU_INFO}")
        return DEFAULT_ECU_INFO


ecu_info = parse_ecu_info(ecu_info_file=app_config.ECU_INFO_FPATH)
