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


from typing import Type

from .interface import (
    StandbySlotCreatorProtocol,
    UpdateMeta,
)
from ..configs import CreateStandbyMechanism, config as cfg
from .. import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


def get_standby_slot_creator(
    mode: CreateStandbyMechanism,
) -> Type[StandbySlotCreatorProtocol]:
    logger.info(f"use slot update {mode=}")
    if mode == CreateStandbyMechanism.LEGACY:
        from .legacy_mode import LegacyMode

        return LegacyMode
    elif mode == CreateStandbyMechanism.REBUILD:
        from .rebuild_mode import RebuildMode

        return RebuildMode
    else:
        raise NotImplementedError(f"slot update {mode=} not implemented")


__all__ = (
    "UpdateMeta",
    "get_standby_slot_creator",
)
