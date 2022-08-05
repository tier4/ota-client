from app.configs import config as cfg
from app.proto import wrapper
from app import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class LiveOTAStatus:
    def __init__(self, ota_status: wrapper.StatusOta) -> None:
        self.live_ota_status = ota_status

    def get_ota_status(self) -> wrapper.StatusOta:
        return self.live_ota_status

    def set_ota_status(self, _status: wrapper.StatusOta):
        self.live_ota_status = _status

    def request_update(self) -> bool:
        return self.live_ota_status in [
            wrapper.StatusOta.INITIALIZED,
            wrapper.StatusOta.SUCCESS,
            wrapper.StatusOta.FAILURE,
            wrapper.StatusOta.ROLLBACK_FAILURE,
        ]

    def request_rollback(self) -> bool:
        return self.live_ota_status in [
            wrapper.StatusOta.SUCCESS,
            wrapper.StatusOta.ROLLBACK_FAILURE,
        ]
