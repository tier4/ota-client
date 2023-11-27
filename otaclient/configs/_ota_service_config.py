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
"""otaclient grpc server config.

For compatibility reason, this config is NOT configurable via env vars.
"""


from __future__ import annotations
from pydantic import Field, BaseModel, ConfigDict, IPvAnyAddress
from typing import Literal, Union


class OTAServiceConfig(BaseModel):
    """Configurable configs for OTA grpc server/client call."""

    model_config = ConfigDict(frozen=True, validate_default=True)

    # used when listen_addr is not configured in ecu_info.yaml.
    DEFAULT_SERVER_ADDRESS: Union[IPvAnyAddress, Literal["127.0.0.1"]] = "127.0.0.1"

    SERVER_PORT: int = Field(default=50051, ge=0, le=65535)
    CLIENT_CALL_PORT: int = Field(default=50051, ge=0, le=65535)


service_config = OTAServiceConfig()
