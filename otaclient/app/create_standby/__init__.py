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


import logging
from typing import Type

from .interface import StandbySlotCreatorProtocol
from ..configs import CreateStandbyMechanism

logger = logging.getLogger(__name__)


def get_standby_slot_creator(
    mode: CreateStandbyMechanism,
) -> Type[StandbySlotCreatorProtocol]:
    logger.info(f"use slot update {mode=}")
    if mode == CreateStandbyMechanism.REBUILD:
        from .rebuild_mode import RebuildMode

        return RebuildMode
    else:
        raise NotImplementedError(f"slot update {mode=} not implemented")


__all__ = ("get_standby_slot_creator",)
