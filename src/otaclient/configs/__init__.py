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
"""otaclient configs package."""

from typing import TYPE_CHECKING, Any

from otaclient.configs._cfg_configurable import ConfigurableSettings, cfg_configurable
from otaclient.configs._cfg_consts import Consts, cfg_consts
from otaclient.configs._ecu_info import BootloaderType, ECUContact, ECUInfo, ecu_info
from otaclient.configs._proxy_info import ProxyInfo, proxy_info

__all__ = [
    "BootloaderType",
    "ECUContact",
    "ECUInfo",
    "ecu_info",
    "ProxyInfo",
    "proxy_info",
    "cfg",
]

if TYPE_CHECKING:

    class _OTAClientConfigs(ConfigurableSettings, Consts):
        """OTAClient configs."""

else:

    class _OTAClientConfigs:

        def __getattribute__(self, name: str) -> Any:
            for _cfg in [cfg_consts, cfg_configurable]:
                try:
                    return getattr(_cfg, name)
                except AttributeError:
                    continue
            raise AttributeError(f"no such config field: {name=}")


cfg = _OTAClientConfigs()
