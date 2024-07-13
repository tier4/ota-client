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
"""otaclient package scope configs."""


from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

from ._app_config import (
    AdvancedOTAClientConfiguration,
    CommonOTAClientConfig,
    LoggingConfig,
)
from ._consts import BootloaderType, CreateStandbyMechanism, consts
from ._ecu_info import parse_ecu_info
from ._paths import _DynamicPathConsts, _StaticPathConsts
from ._proxy_info import parse_proxy_info

__all__ = [
    "consts",
    "CreateStandbyMechanism",
    "BootloaderType",
    "OTAClientConfig",
    "app_cfg",
    "static_paths",
    "dynamic_paths",
    "ecu_info",
    "proxy_info",
]


class OTAClientConfig(
    CommonOTAClientConfig, AdvancedOTAClientConfiguration, LoggingConfig
): ...


COMMON_OPT_PREFIX = "OTA_"
ADVANCED_OPT_PREFIX = "OTA_ADVANCE_"


def _parse_configs():
    """Parse otaclient configs from environmental variables."""

    class _ParseCommonConfig(BaseSettings, CommonOTAClientConfig, LoggingConfig):
        model_config = SettingsConfigDict(
            env_prefix=COMMON_OPT_PREFIX,
            frozen=True,
            validate_default=True,
            use_enum_values=True,
        )

    class _ParseAdvanceConfig(BaseSettings, AdvancedOTAClientConfiguration):
        model_config = SettingsConfigDict(
            env_prefix=ADVANCED_OPT_PREFIX,
            frozen=True,
            validate_default=True,
            use_enum_values=True,
        )

    return OTAClientConfig(
        **_ParseCommonConfig().model_dump(), **_ParseAdvanceConfig().model_dump()
    )


#
# ------ instantiate config objects ------ #
#
app_cfg = _parse_configs()
static_paths = _StaticPathConsts()
dynamic_paths = _DynamicPathConsts(host_rootfs=app_cfg.HOST_ROOTFS)
ecu_info = parse_ecu_info(dynamic_paths.ECU_INFO_FPATH)
proxy_info = parse_proxy_info(dynamic_paths.PROXY_INFO_FPATH)
