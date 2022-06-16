from typing import Type

from app.create_standby.common import (
    StandbySlotCreatorProtocol,
    UpdateMeta,
    CreateStandbySlotExternalError,
    CreateStandbySlotInternalError,
    CreateStandbySlotError,
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


_AUOTSELECTED_MODE = select_mode()


def get_standby_creator(
    mode: str = _AUOTSELECTED_MODE,
) -> Type[StandbySlotCreatorProtocol]:
    logger.info(f"use slot update {mode=}")
    if mode == "legacy":
        from app.create_standby.legacy_mode import LegacyMode

        return LegacyMode
    elif mode == "rebuild":
        from app.create_standby.rebuild_mode import RebuildMode

        return RebuildMode
    else:
        raise NotImplementedError(f"slot update {mode=} not implemented")


def get_reference_slot(
    mode: str = _AUOTSELECTED_MODE, *, cur_slot: str, standby_slot: str
):
    """Get the bank to copy from."""
    if mode in ("legacy", "rebuild"):
        res = cur_slot
    elif mode in ("in_place"):
        res = standby_slot
    else:
        raise NotImplementedError(f"slot update {mode=} not implemented")

    logger.info(f"use {res} as reference slot for local copying")
    return res


StandbySlotCreator: Type[StandbySlotCreatorProtocol] = get_standby_creator()

__all__ = (
    "StandBySlotCreator",
    "CreateStandbySlotError",
    "CreateStandbySlotExternalError",
    "CreateStandbySlotInternalError",
    "UpdateMeta",
    "get_reference_bank",
    "get_bank_creator",
)
