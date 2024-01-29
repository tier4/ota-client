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
from pydantic import BaseModel, Field
from typing import Any, ClassVar, List, Iterator

from otaclient._utils.typing import StrOrPath
from otaclient.configs.app_cfg import app_config
from otaclient.configs.ota_service_cfg import service_config

logger = logging.getLogger(__name__)

DEFAULT_ECU_INFO = {
    "format_version": 1,  # current version is 1
    "ecu_id": "autoware",  # should be unique for each ECU in vehicle
}


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


class ECUContact(BaseModel):
    ecu_id: str
    ip_addr: str
    port: int = Field(default=service_config.CLIENT_CALL_PORT, gt=0, lt=65535)


class ECUInfo(BaseModel):
    format_version: int = 1
    ecu_id: str
    ip_addr: str = str(service_config.DEFAULT_SERVER_ADDRESS)
    bootloader: BootloaderType = BootloaderType.UNSPECIFIED
    available_ecu_ids: List[str] = Field(default_factory=list)
    secondaries: List[ECUContact] = Field(default_factory=list)

    def iter_direct_subecu_contact(self) -> Iterator[ECUContact]:
        yield from self.secondaries

    def get_bootloader(self) -> BootloaderType:
        return self.bootloader

    def get_available_ecu_ids(self) -> list[str]:
        # onetime fix, if no availabe_ecu_id is specified,
        # add my_ecu_id into the list
        if len(self.available_ecu_ids) == 0:
            return [self.ecu_id]
        return self.available_ecu_ids.copy()


def parse_ecu_info(ecu_info_file: StrOrPath) -> ECUInfo:
    try:
        _raw_yaml_str = Path(ecu_info_file).read_text()
        loaded_ecu_info = yaml.safe_load(_raw_yaml_str)
        assert isinstance(loaded_ecu_info, dict), "not a valid yaml file"

        return ECUInfo.model_validate(loaded_ecu_info, strict=True)
    except Exception as e:
        logger.warning(f"{ecu_info_file=} is missing or invalid: {e!r}")
        loaded_ecu_info = DEFAULT_ECU_INFO
        logger.warning(f"use default ecu_info: {loaded_ecu_info}")

        return ECUInfo.model_validate(loaded_ecu_info)


ecu_info = parse_ecu_info(ecu_info_file=app_config.ECU_INFO_FPATH)
