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

from abc import abstractmethod
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from otaclient.boot_control.protocol import BootControllerProtocol
    from otaclient.create_standby.interface import StandbySlotCreatorProtocol
    from otaclient_api.v2 import types as api_types


class OTAClientProtocol(Protocol):
    def __init__(
        self,
        *,
        boot_control_cls: type[BootControllerProtocol],
        create_standby_cls: type[StandbySlotCreatorProtocol],
        my_ecu_id: str = "",
    ) -> None: ...

    @abstractmethod
    def update(
        self,
        version: str,
        url_base: str,
        cookies_json: str,
    ) -> None: ...

    @abstractmethod
    def rollback(self) -> None: ...

    @abstractmethod
    def status(self) -> api_types.StatusResponseEcuV2: ...
