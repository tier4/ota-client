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
