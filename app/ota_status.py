from ota_error import OtaErrorBusy
from enum import Enum, unique
from configs import config as cfg
import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


@unique
class OtaStatus(Enum):
    INITIALIZED = 0
    SUCCESS = 1
    FAILURE = 2
    UPDATING = 3
    ROLLBACKING = 4
    ROLLBACK_FAILURE = 5


class OtaStatusControlMixin:
    def _attributes_dependencies(self):
        """
        placeholder method
        attributes that needed for this mixin to work

        these attributes will be initialized in OtaClient
        """
        self._ota_status: OtaStatus = None

    def get_ota_status(self):
        return self._ota_status

    def set_ota_status(self, ota_status):
        logger.info(f"{ota_status=}")
        self._ota_status = ota_status

    def check_update_status(self):
        logger.debug("check if ota_status is valid for updating...")
        # check status
        if self._ota_status not in [
            OtaStatus.INITIALIZED,
            OtaStatus.SUCCESS,
            OtaStatus.FAILURE,
            OtaStatus.ROLLBACK_FAILURE,
        ]:
            raise OtaErrorBusy(f"status={self._ota_status} is illegal for update")

    def check_rollback_status(self):
        # check status
        if self._ota_status not in [
            OtaStatus.SUCCESS,
            OtaStatus.ROLLBACK_FAILURE,
        ]:
            raise OtaErrorBusy(f"status={self._ota_status} is illegal for rollback")
