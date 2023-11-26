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
"""otaclient debug flags settings."""


from __future__ import annotations
from pydantic_settings import BaseSettings, SettingsConfigDict

from . import ENV_PREFIX


class DebugFlags(BaseSettings):
    """Enable internal debug features."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        frozen=True,
        validate_default=True,
    )

    # main DEBUG_MODE switch, this flag will enable all debug feature.
    DEBUG_MODE: bool = False

    # enable failure_traceback field in status API response.
    DEBUG_ENABLE_TRACEBACK_IN_STATUS_API: bool = False


debug_flags = DebugFlags()
