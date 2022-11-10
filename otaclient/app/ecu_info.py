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
from dataclasses import dataclass, field, fields, MISSING
from pathlib import Path
from typing import Iterator, Union, Dict, List, Tuple, Any

from . import log_util
from .configs import config as cfg
from .boot_control import BootloaderType

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


DEFAULT_ECU_INFO = {
    "format_version": 1,  # current version is 1
    "ecu_id": "autoware",  # should be unique for each ECU in vehicle
}


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
    available_ecu_id: List[str] = field(default_factory=list)
    secondaries: List[Dict[str, str]] = field(default_factory=list)
    format_version: int = 1

    @classmethod
    def parse_ecu_info(cls, ecu_info_file: Union[str, Path]) -> "ECUInfo":
        ecu_info = DEFAULT_ECU_INFO.copy()
        try:
            ecu_info = yaml.safe_load(Path(ecu_info_file).read_text())
            assert isinstance(ecu_info, Dict)
        except Exception:
            logger.warning(
                f"failed to load {ecu_info_file=} or config file corrupted, use default config"
            )
        logger.info(f"ecu_info={ecu_info}")

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
                if _field.default is MISSING:
                    raise ValueError(
                        f"required field {_field.name} is not presented, abort"
                    )
                _ecu_info_dict[_field.name] = _field.default
                continue
            if isinstance(_option, (list, dict)):
                _ecu_info_dict[_field.name] = _option.copy()
            else:
                _ecu_info_dict[_field.name] = _option

        # initialize ECUInfo inst
        return cls(**_ecu_info_dict)

    def iter_secondary_ecus(self) -> Iterator[Tuple[str, str]]:
        """
        Return a tuple contains ecu_id and ip_addr in str.
        """
        for subecu in self.secondaries:
            yield subecu["ecu_id"], subecu.get("ip_addr", "127.0.0.1")

    def get_ecu_id(self) -> str:
        return self.ecu_id

    def get_ecu_ip_addr(self) -> str:
        return self.ip_addr

    def get_bootloader(self) -> BootloaderType:
        return BootloaderType.parse_str(self.bootloader)

    def get_available_ecu_ids(self) -> List[str]:
        return self.available_ecu_id.copy()
