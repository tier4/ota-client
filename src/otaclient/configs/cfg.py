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
"""Load ecu_info, proxy_info and otaclient configs."""

from typing import TYPE_CHECKING, Any

from otaclient.configs._cfg_configurable import ConfigurableSettings, set_configs
from otaclient.configs._cfg_consts import Consts
from otaclient.configs._ecu_info import (
    BootloaderType,
    ECUEndpoint,
    ECUInfo,
    parse_ecu_info,
)
from otaclient.configs._proxy_info import ProxyInfo, parse_proxy_info

__all__ = [
    "BootloaderType",
    "ECUEndpoint",
    "ECUInfo",
    "ecu_info",
    "ProxyInfo",
    "proxy_info",
    "cfg",
]

cfg_configurable = set_configs()
cfg_consts = Consts()

ECU_INFO_LOADED_SUCCESSFULLY: bool
"""A const set at startup time ecu_info.yaml parsing.

If it is False, it means that the ecu_info.yaml file is invalid,
    and the default ecu_info(defined in _ecu_info module) is used.
"""
PROXY_INFO_LOADED_SUCCESSFULLY: bool
"""A const set at startup time proxy_info.yaml parsing.

If it is False, it means that the proxy_info.yaml file is invalid,
    and the default proxy_info(defined in _proxy_info module) is used.
"""

if TYPE_CHECKING:

    class _OTAClientConfigs(ConfigurableSettings, Consts):
        """OTAClient configs."""

else:

    class _OTAClientConfigs:

        # NOTE(20241108): still use __getattr__ to allow changing/mocking attributes
        #   for easy testing.
        def __getattr__(self, name: str) -> Any:
            for _cfg in [cfg_consts, cfg_configurable]:
                try:
                    return getattr(_cfg, name)
                except AttributeError:
                    continue
            raise AttributeError(f"no such config field: {name=}")


cfg = _OTAClientConfigs()
ECU_INFO_LOADED_SUCCESSFULLY, ecu_info = parse_ecu_info(
    ecu_info_file=cfg.ECU_INFO_FPATH
)
PROXY_INFO_LOADED_SUCCESSFULLY, proxy_info = parse_proxy_info(
    proxy_info_file=cfg.PROXY_INFO_FPATH
)
