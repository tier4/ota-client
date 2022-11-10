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
# available options: grub, cboot, rpi_boot
bootloader: "grub"
secondaries:
    - ecu_id: "p1"
      ip_addr: "0.0.0.0"
available_ecu_ids:
    - "autoware"
    - "p1"
"""

import yaml
from pathlib import Path
from typing import Iterator, Union, Dict, List, Tuple

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


class ECUInfo:
    def __init__(self, ecu_info_file: Union[str, Path]):
        ecu_info = DEFAULT_ECU_INFO
        try:
            with open(ecu_info_file) as f:
                ecu_info = yaml.safe_load(f)
        except Exception:
            pass
        logger.info(f"ecu_info={ecu_info}")
        # required field
        self.ecu_id = ecu_info["ecu_id"]
        # optional fields
        self.ip_addr = ecu_info.get("ip_addr", "127.0.0.1")
        self.bootloader_type = BootloaderType.parse_str(
            ecu_info.get("bootloader", "unspecified")
        )
        self.secondaries: List[Dict[str, str]] = ecu_info.get("secondaries", [])
        self.available_ecu_id: List[str] = ecu_info.get(
            "available_ecu_ids", [self.ecu_id]
        )

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

    def get_available_ecu_ids(self) -> List[str]:
        return self.available_ecu_id.copy()
