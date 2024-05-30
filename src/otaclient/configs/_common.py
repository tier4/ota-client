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


from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from pydantic_settings import BaseSettings, SettingsConfigDict

# prefix for environmental vars name for configs.
ENV_PREFIX = "OTA_"


class BaseConfigurableConfig(BaseSettings):
    """Common base for configs that are configurable via ENV."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        frozen=True,
        validate_default=True,
    )


class BaseFixedConfig(BaseModel):
    """Common base for configs that should be fixed and not changable."""

    model_config = ConfigDict(frozen=True, validate_default=True)
