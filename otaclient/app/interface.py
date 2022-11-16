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


from abc import abstractmethod
from typing import Protocol, Type

from .boot_control.protocol import BootControllerProtocol
from .create_standby.interface import StandbySlotCreatorProtocol
from .proto import otaclient_v2_pb2 as v2


class OTAClientProtocol(Protocol):
    def __init__(
        self,
        *,
        boot_control_cls: Type[BootControllerProtocol],
        create_standby_cls: Type[StandbySlotCreatorProtocol],
        my_ecu_id: str = "",
    ) -> None:
        ...

    @abstractmethod
    def update(
        self,
        version: str,
        url_base: str,
        cookies_json: str,
        *,
        fsm,  # OTAUpdateFSM
    ) -> None:
        ...

    @abstractmethod
    def rollback(self) -> None:
        ...

    @abstractmethod
    def status(self) -> v2.StatusResponseEcu:
        ...
