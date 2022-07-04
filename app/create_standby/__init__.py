from typing import Type

from app.create_standby.interface import (
    StandbySlotCreatorProtocol,
    UpdateMeta,
)
from app.configs import config as cfg
from app import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


def select_mode() -> str:
    # TODO: select mode mechanism
    if cfg.STANDBY_CREATION_MODE == "auto":
        return "rebuild"

    return cfg.STANDBY_CREATION_MODE


AUOTSELECTED_MODE = select_mode()


def get_standby_slot_creator(mode: str) -> Type[StandbySlotCreatorProtocol]:
    logger.info(f"use slot update {mode=}")
    if mode == "legacy":
        from app.create_standby.legacy_mode import LegacyMode

        return LegacyMode
    elif mode == "rebuild":
        from app.create_standby.rebuild_mode import RebuildMode

        return RebuildMode
    else:
        raise NotImplementedError(f"slot update {mode=} not implemented")


__all__ = (
    "UpdateMeta",
    "AUOTSELECTED_MODE",
    "get_standby_slot_creator",
)
