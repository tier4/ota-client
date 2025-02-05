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

from typing import TYPE_CHECKING

from otaclient.configs._cfg_configurable import (
    ENV_PREFIX,
    ConfigurableSettings,
    set_configs,
)
from otaclient.configs._cfg_consts import Consts, CreateStandbyMechanism, dynamic_root
from otaclient.configs._ecu_info import BootloaderType, ECUContact, ECUInfo
from otaclient.configs._proxy_info import ProxyInfo

__all__ = [
    "ENV_PREFIX",
    "ConfigurableSettings",
    "Consts",
    "CreateStandbyMechanism",
    "BootloaderType",
    "ECUContact",
    "ECUInfo",
    "ProxyInfo",
    "set_configs",
    "dynamic_root",
    "DefaultOTAClientConfigs",
]

if TYPE_CHECKING:

    class DefaultOTAClientConfigs(ConfigurableSettings, Consts):
        """Default OTAClient configs, without parsing the env vars."""

else:

    class DefaultOTAClientConfigs:

        def __init__(self) -> None:
            self._cfg_consts = Consts()
            self._cfg_configurable = ConfigurableSettings()

        # NOTE(20241108): still use __getattr__ to allow changing/mocking attributes
        #   for easy testing.
        def __getattr__(self, name: str):
            for _cfg in [self._cfg_consts, self._cfg_configurable]:
                try:
                    return getattr(_cfg, name)
                except AttributeError:
                    continue
            raise AttributeError(f"no such config field: {name=}")
