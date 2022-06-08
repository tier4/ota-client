from pathlib import Path

from app.common import read_from_file, write_to_file
from app.configs import config as cfg
from app.interface import OTAStatusHandlerProtocol, OtaStatus
from app.ota_error import OtaErrorBusy
from app import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class OTAStatusHandler(OTAStatusHandlerProtocol):
    def __init__(
        self, *, standby_slot_status_file: Path, current_slot_status_file: Path
    ) -> None:
        self.standby_slot_status_file: Path = standby_slot_status_file
        self.current_slot_status_file: Path = current_slot_status_file
        self._live_ota_status: OtaStatus = None

    def init_ota_status(self):
        """Apply INITIALIZED status if ota_status is not configured yet."""
        write_to_file(self.current_slot_status_file, OtaStatus.INITIALIZED.name)

    def get_current_ota_status(self) -> OtaStatus:
        _status = read_from_file(self.current_slot_status_file)
        return OtaStatus[_status.upper()]

    def get_live_ota_status(self) -> OtaStatus:
        return self._live_ota_status

    def set_live_ota_status(self, _status: OtaStatus):
        self._ota_status = _status

    def store_standby_ota_status(self, _status: OtaStatus):
        write_to_file(self.standby_slot_status_file, _status.name)

    def request_update(self) -> None:
        if self._ota_status not in [
            OtaStatus.INITIALIZED,
            OtaStatus.SUCCESS,
            OtaStatus.FAILURE,
            OtaStatus.ROLLBACK_FAILURE,
        ]:
            raise OtaErrorBusy(f"status={self._ota_status} is illegal for update")

    def request_rollback(self) -> None:
        if self._ota_status not in [
            OtaStatus.SUCCESS,
            OtaStatus.ROLLBACK_FAILURE,
        ]:
            raise OtaErrorBusy(f"status={self._ota_status} is illegal for rollback")
