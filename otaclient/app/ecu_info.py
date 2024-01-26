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
from typing_extensions import Self

from otaclient._utils.typing import StrOrPath
from otaclient.configs.ota_service_cfg import service_config

logger = logging.getLogger(__name__)

DEFAULT_ECU_INFO = {
    "format_version": 1,  # current version is 1
    "ecu_id": "autoware",  # should be unique for each ECU in vehicle
}


class BootloaderType(str, Enum):
    """Bootloaders that supported by otaclient.
    This class is copied from app.boot_control as it.
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
    format_version: ClassVar[int] = 1
    ecu_id: str
    ip_addr: str = str(service_config.DEFAULT_SERVER_ADDRESS)
    bootloader: BootloaderType = BootloaderType.UNSPECIFIED
    available_ecu_ids: List[str] = Field(default_factory=list)
    secondaries: List[ECUContact] = Field(default_factory=list)

    @classmethod
    def parse_ecu_info(cls, ecu_info_file: StrOrPath) -> Self:
        try:
            _raw_yaml_str = Path(ecu_info_file).read_text()
        except FileNotFoundError as e:
            logger.error(f"{e!r}")
            raise

        try:
            ecu_info_dict: dict[str, Any] = yaml.safe_load(_raw_yaml_str)
            assert isinstance(ecu_info_dict, dict), "not a valid ecu_info.yaml"

            return cls.model_validate(ecu_info_dict)
        except Exception as e:
            logger.warning(f"{ecu_info_file=} is invalid, use default config: {e!r}")
            logger.warning(f"invalid ecu_info.yaml contenxt: {_raw_yaml_str}")
            return cls.model_validate(DEFAULT_ECU_INFO)

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
