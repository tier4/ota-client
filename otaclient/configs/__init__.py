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


from ._debug_setting import debug_flags
from ._logging import logging_config
from ._ota_service import service_config

# prefix for environmental vars name for configs.
ENV_PREFIX = "OTA_"

__all__ = ["ENV_PREFIX", "debug_flags", "logging_config", "service_config"]
