from enum import Enum, unique
from pathlib import Path
import subprocess
import shlex
import shutil

from ota_partition import OtaPartitionFile


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

    def get_standby_boot_partition_path(self):
        return self._ota_partition.get_standby_boot_partition_path()

    def enter_updating(self, version, mount_path: Path):
        # check status
        if self._ota_status not in [
            OtaStatus.INITIALIZED,
            OtaStatus.SUCCESS,
            OtaStatus.FAILURE,
            OtaStatus.ROLLBACK_FAILURE,
        ]:
            raise ValueError(f"status={self.status} is illegal for update")

        self._ota_status = OtaStatus.UPDATING

        self._ota_partition.store_standby_ota_status(OtaStatus.UPDATING.name)
        self._ota_partition.store_standby_ota_version(version)
        self._ota_partition.cleanup_standby_boot_partition()
        self._ota_partition.mount_standby_root_partition_and_clean(mount_path)

    def leave_updating(self, mounted_path: Path):
        self._ota_partition.update_fstab(mounted_path)
        self._ota_partition.create_custom_cfg_and_reboot()

    def enter_rollbacking(self):
        # FIXME: not implemented yet
        # check status
        if self.ota_status not in [
            OtaStatus.SUCCESS,
            OtaStatus.ROLLBACK_FAILURE,
        ]:
            raise ValueError(f"status={self.status} is illegal for rollback")

        self._ota_status = OtaStatus.ROLLBACKING

        self._ota_partition.store_standby_ota_status(OtaStatus.ROLLBACKING.name)

    def leave_rollbacking(self):
        # FIXME: not implemented yet
        self._grub_control.create_custom_cfg_and_reboot()

    """ private functions from here """

    def _initialize_ota_status(self):
        status_string = self._ota_partition.load_ota_status()
        if status_string == "":
            self._ota_partition.store_standby_ota_status(OtaStatus.INITIALIZED.name)
            return OtaStatus.INITIALIZED
        if status_string == OtaStatus.UPDATING.name:
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
