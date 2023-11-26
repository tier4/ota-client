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
"""otaclient grpc server config."""


from __future__ import annotations
from pydantic import IPvAnyAddress, Field
from typing import Optional

from ._common import BaseConfig


class OTAServiceConfig(BaseConfig):
    """Configurable configs for OTA grpc server/client call.

    NOTE: for SERVER_ADDRESS, normally this value is not needed to be configured,
        as this setting by default is configured in ecu_info.yaml.
        The setting here is for advanced use case when we need to make server listen
        on different address without changing ecu_info.yaml.
    """

    # NOTE: SERVER_ADDRESS specified here will supersede the value comes from ecu_info.yaml,
    #       only specify it for advanced use case!
    SERVER_ADDRESS: Optional[IPvAnyAddress] = None
    SERVER_PORT: int = Field(default=50051, ge=0, le=65535)

    CLIENT_CALL_PORT: int = Field(default=50051, ge=0, le=65535)


service_config = OTAServiceConfig()
