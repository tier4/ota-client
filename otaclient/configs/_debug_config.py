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
"""otaclient debug flags settings.

Debug configs is configurable via environmental configs.

Debug configs consists of two types of configs:
1. flag: which enables/disables specific feature at runtime,
2. override_config: which overrides specific config at runtime.

If the main DEBUG_MODE flag is enable, all flag type debug configs
will be enabled. 

For override_config type config, it will be enabled if this config
has assigned correct value via environmental var.
"""


from __future__ import annotations
from pydantic import IPvAnyAddress
from typing import Optional
from ._common import BaseConfig


class DebugFlags(BaseConfig):
    """Enable internal debug features."""

    # main DEBUG_MODE switch, this flag will enable all debug feature.
    DEBUG_MODE: bool = False

    # enable failure_traceback field in status API response.
    DEBUG_ENABLE_TRACEBACK_IN_STATUS_API: bool = False

    # override otaclient grpc server listen address
    DEBUG_SERVER_LISTEN_ADDR: Optional[IPvAnyAddress] = None


debug_flags = DebugFlags()
