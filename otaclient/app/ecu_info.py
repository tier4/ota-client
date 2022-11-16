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


import yaml

from . import log_setting
from .configs import config as cfg

logger = log_setting.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class EcuInfo:
    ECU_INFO_FILE = cfg.ECU_INFO_FILE
    DEFAULT_ECU_INFO = {
        "format_version": 1,  # current version is 1
        "ecu_id": "autoware",  # should be unique for each ECU in vehicle
    }

    def __init__(self):
        ecu_info_file = EcuInfo.ECU_INFO_FILE
        self._ecu_info = self._load_ecu_info(ecu_info_file)
        logger.info(f"ecu_info={self._ecu_info}")

    def get_secondary_ecus(self):
        return self._ecu_info.get("secondaries", [])

    def get_ecu_id(self):
        return self._ecu_info["ecu_id"]

    def get_ecu_ip_addr(self):
        return self._ecu_info.get("ip_addr", "localhost")

    def get_available_ecu_ids(self):
        return self._ecu_info.get("available_ecu_ids", [self.get_ecu_id()])

    def _load_ecu_info(self, path: str):
        try:
            with open(path) as f:
                ecu_info = yaml.safe_load(f)
        except Exception:
            return EcuInfo.DEFAULT_ECU_INFO
        return ecu_info
