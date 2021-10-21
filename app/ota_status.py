from enum import Enum, unique
from pathlib import Path

from ota_partition import OtaPartitionFile
from ota_error import OtaErrorRecoverable
import configs as cfg
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


class OtaStatusControl:
    def __init__(self):
        self._ota_partition = OtaPartitionFile()
        self._ota_status = self._initialize_ota_status()

    def get_ota_status(self):
        return self._ota_status

    def set_ota_status(self, ota_status):
        self._ota_status = ota_status
        self._ota_partition.store_standby_ota_status(ota_status.name)

    def get_version(self):
        return self._ota_partition.load_ota_version()

    def get_standby_boot_partition_path(self):
        return self._ota_partition.get_standby_boot_partition_path()

    def check_status_for_ota(self):
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

    def enter_updating(self, version, mount_path: Path):
        self.check_status_for_ota()

        self._ota_status = OtaStatus.UPDATING

        self._ota_partition.store_standby_ota_status(OtaStatus.UPDATING.name)
        self._ota_partition.store_standby_ota_version(version)
        self._ota_partition.cleanup_standby_boot_partition()
        self._ota_partition.mount_standby_root_partition_and_clean(mount_path)

    def leave_updating(self, mounted_path: Path):
        self._ota_partition.update_fstab(mounted_path)
        self._ota_partition.create_custom_cfg_and_reboot()

    def enter_rollbacking(self):
        # check status
        if self._ota_status not in [
            OtaStatus.SUCCESS,
            OtaStatus.ROLLBACK_FAILURE,
        ]:
            raise OtaErrorRecoverable(
                f"status={self._ota_status} is illegal for rollback"
            )

        self._ota_status = OtaStatus.ROLLBACKING

        self._ota_partition.store_standby_ota_status(OtaStatus.ROLLBACKING.name)

    def leave_rollbacking(self):
        self._ota_partition.create_custom_cfg_and_reboot(rollback=True)

    """ private functions from here """

    def _initialize_ota_status(self):
        status_string = self._ota_partition.load_ota_status()
        if status_string == "":
            self._ota_partition.store_standby_ota_status(OtaStatus.INITIALIZED.name)
            return OtaStatus.INITIALIZED
        if status_string in [OtaStatus.UPDATING.name, OtaStatus.ROLLBACKING.name]:
            if self._ota_partition.is_switching_boot_partition_from_active_to_standby():
                self._ota_partition.store_active_ota_status(OtaStatus.SUCCESS.name)
                self._ota_partition.update_grub_cfg()
                # switch should be called last.
                self._ota_partition.switch_boot_partition_from_active_to_standby()
                return OtaStatus.SUCCESS
            else:
                self._ota_partition.store_standby_ota_status(OtaStatus.FAILURE.name)
                return OtaStatus.FAILURE
        else:
            return OtaStatus[status_string]
