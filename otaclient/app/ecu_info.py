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
r"""ECU metadatas definition."""

import yaml
from copy import deepcopy
from dataclasses import dataclass, field, fields, MISSING
from pathlib import Path
from typing import Iterator, NamedTuple, Union, Dict, List, Any

from . import log_setting
from .configs import service_config
from .boot_control import BootloaderType

logger = log_setting.get_logger(__name__)


DEFAULT_ECU_INFO = {
    "format_version": 1,  # current version is 1
    "ecu_id": "autoware",  # should be unique for each ECU in vehicle
}


class ECUContact(NamedTuple):
    ecu_id: str
    host: str
    port: int = service_config.CLIENT_CALL_PORT


@dataclass
class ECUInfo:
    """
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

    ecu_id: str
    ip_addr: str = "127.0.0.1"
    bootloader: str = BootloaderType.UNSPECIFIED.value
    available_ecu_ids: list = field(default_factory=list)  # list[str]
    secondaries: list = field(default_factory=list)  # list[dict[str, Any]]
    format_version: int = 1

    @classmethod
    def parse_ecu_info(cls, ecu_info_file: Union[str, Path]) -> "ECUInfo":
        ecu_info = deepcopy(DEFAULT_ECU_INFO)
        try:
            ecu_info_yaml = Path(ecu_info_file).read_text()
            _ecu_info = yaml.safe_load(ecu_info_yaml)
            assert isinstance(_ecu_info, Dict)
            ecu_info = _ecu_info
        except (yaml.error.MarkedYAMLError, AssertionError) as e:
            logger.warning(
                f"invalid {ecu_info_yaml=}, use default config: {e!r}"  # type: ignore
            )
        except Exception as e:
            logger.warning(
                f"{ecu_info_file=} not found or unexpected err, use default config: {e!r}"
            )
        logger.info(f"parsed {ecu_info=}")

        # load options
        # NOTE: if option is not presented,
        #       this option will be set to the default value
        _ecu_info_dict: Dict[str, Any] = dict()
        for _field in fields(cls):
            _option = ecu_info.get(_field.name)
            if not isinstance(_option, _field.type):
                if _option is not None:
                    logger.warning(
                        f"{_field.name} contains invalid value={_option}, "
                        "ignored and set to default={_field.default}"
                    )
                if _field.default is MISSING and _field.default_factory is MISSING:
                    raise ValueError(
                        f"required field {_field.name} is not presented, abort"
                    )
                _ecu_info_dict[_field.name] = (
                    _field.default
                    if _field.default is not MISSING
                    else _field.default_factory()  # type: ignore
                )
            # parsed _option is available
            else:
                _ecu_info_dict[_field.name] = _option

        # initialize ECUInfo inst
        return cls(**deepcopy(_ecu_info_dict))

    def iter_direct_subecu_contact(self) -> Iterator[ECUContact]:
        for subecu in self.secondaries:
            try:
                yield ECUContact(
                    ecu_id=subecu["ecu_id"],
                    host=subecu["ip_addr"],
                    port=subecu.get("port", service_config.CLIENT_CALL_PORT),
                )
            except KeyError:
                raise ValueError(f"{subecu=} info is invalid")

    def get_bootloader(self) -> BootloaderType:
        return BootloaderType.parse_str(self.bootloader)

    def get_available_ecu_ids(self) -> List[str]:
        # onetime fix, if no availabe_ecu_id is specified,
        # add my_ecu_id into the list
        if len(self.available_ecu_ids) == 0:
            self.available_ecu_ids.append(self.ecu_id)
        return self.available_ecu_ids.copy()
