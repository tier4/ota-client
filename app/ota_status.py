from ota_error import OtaErrorRecoverable
from enum import Enum, unique


@unique
class OtaStatus(Enum):
    INITIALIZED = 0
    SUCCESS = 1
    FAILURE = 2
    UPDATING = 3
    ROLLBACKING = 4
    ROLLBACK_FAILURE = 5


class OtaStatusControlMixin:
    _ota_status = None  # initialized by boot_control

    def get_ota_status(self):
        return self._ota_status

    def set_ota_status(self, ota_status):
        self._ota_status = ota_status

    def check_update_status(self):
        # check status
        if self._ota_status not in [
            OtaStatus.INITIALIZED,
            OtaStatus.SUCCESS,
            OtaStatus.FAILURE,
            OtaStatus.ROLLBACK_FAILURE,
        ]:
            raise OtaErrorRecoverable(
                f"status={self._ota_status} is illegal for update"
            )

    def check_rollback_status(self):
        # check status
        if self._ota_status not in [
            OtaStatus.SUCCESS,
            OtaStatus.ROLLBACK_FAILURE,
        ]:
            raise OtaErrorRecoverable(
                f"status={self._ota_status} is illegal for rollback"
            )
