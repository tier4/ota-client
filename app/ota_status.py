from enum import Enum, unique, auto
from pathlib import Path

from app.common import read_from_file, write_to_file
from app.configs import config as cfg
from app.ota_error import OtaErrorBusy
from app import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


@unique
class OTAStatusEnum(Enum):
    INITIALIZED = 0
    SUCCESS = auto()
    FAILURE = auto()
    UPDATING = auto()
    ROLLBACKING = auto()
    ROLLBACK_FAILURE = auto()


class OTAStatusMixin:
    live_ota_status: OTAStatusEnum

    def get_ota_status(self) -> OTAStatusEnum:
        return self.live_ota_status

    def set_ota_status(self, _status: OTAStatusEnum):
        self.live_ota_status = _status

    def request_update(self) -> None:
        if self.live_ota_status not in [
            OTAStatusEnum.INITIALIZED,
            OTAStatusEnum.SUCCESS,
            OTAStatusEnum.FAILURE,
            OTAStatusEnum.ROLLBACK_FAILURE,
        ]:
            raise OtaErrorBusy(f"{self.live_ota_status=} is illegal for update")

    def request_rollback(self) -> None:
        if self.live_ota_status not in [
            OTAStatusEnum.SUCCESS,
            OTAStatusEnum.ROLLBACK_FAILURE,
        ]:
            raise OtaErrorBusy(f"{self.live_ota_status=} is illegal for rollback")
