from enum import Enum, unique, auto

from app.configs import config as cfg
from app import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


@unique
class OTAStatusEnum(Enum):
    INITIALIZED = 0
    SUCCESS = 1
    FAILURE = 2
    UPDATING = 3
    ROLLBACKING = 4
    ROLLBACK_FAILURE = 5


class LiveOTAStatus:
    def __init__(self, ota_status: OTAStatusEnum) -> None:
        self.live_ota_status = ota_status

    def get_ota_status(self) -> OTAStatusEnum:
        return self.live_ota_status

    def set_ota_status(self, _status: OTAStatusEnum):
        self.live_ota_status = _status

    def request_update(self) -> bool:
        return self.live_ota_status in [
            OTAStatusEnum.INITIALIZED,
            OTAStatusEnum.SUCCESS,
            OTAStatusEnum.FAILURE,
            OTAStatusEnum.ROLLBACK_FAILURE,
        ]

    def request_rollback(self) -> bool:
        return self.live_ota_status in [
            OTAStatusEnum.SUCCESS,
            OTAStatusEnum.ROLLBACK_FAILURE,
        ]
