# platform specific codes for main_ecu(host PC)
import ota_partition
from configs import Config as cfg
from pathlib import Path
from ota_status import OtaStatus
from boot_control import BootControlMixinInterface


class BootControlMixin(BootControlMixinInterface):
    _boot_control: ota_partition.OtaPartitionFile = None
    _mount_point: Path = None

    def initialize_ota_status(self):
        status_string = self.load_ota_status()
        if status_string == "":
            self.write_standby_ota_status(OtaStatus.INITIALIZED.name)
            return OtaStatus.INITIALIZED
        elif status_string == OtaStatus.UPDATING.name:
            return self.finalize_update()
        elif status_string == OtaStatus.ROLLBACKING.name:
            return self.finalize_rollback()
        else:
            return OtaStatus[status_string]

    def write_standby_ota_status(self, status):
        self._boot_control.store_standby_ota_status(status)

    def write_initialized_ota_status(self):
        self.write_standby_ota_status(OtaStatus.INITIALIZED.name)
        return OtaStatus.INITIALIZED

    def write_ota_status(self, status):
        self._boot_control.store_standby_ota_status(status)

    def load_ota_status(self):
        return self._boot_control.load_ota_status()

    def get_standby_boot_partition_path(self) -> Path:
        return self._boot_control.get_standby_boot_partition_path()

    def get_version(self):
        return self._boot_control.load_ota_version()

    def boot_ctrl_pre_update(self, version):
        self.write_env("status", OtaStatus.UPDATING.name)
        self.write_env("version", version)

        self._boot_control.cleanup_standby_boot_partition()
        self._boot_control.mount_standby_root_partition_and_clean(self._mount_point)

    def boot_ctrl_post_update(self):
        self._boot_control.update_fstab(self._mount_point)
        self._boot_control.create_custom_cfg_and_reboot()

    def boot_ctrl_pre_rollback(self):
        self._boot_control.store_standby_ota_status(OtaStatus.ROLLBACKING.name)

    def boot_ctrl_post_rollback(self):
        self._boot_control.create_custom_cfg_and_reboot(rollback=True)

    def finalize_update(self) -> OtaStatus:
        if self._boot_control.is_switching_boot_partition_from_active_to_standby():
            self._boot_control.store_active_ota_status(OtaStatus.SUCCESS.name)
            self._boot_control.update_grub_cfg()
            # switch should be called last.
            self._boot_control.switch_boot_partition_from_active_to_standby()
            return OtaStatus.SUCCESS
        else:
            self._boot_control.store_standby_ota_status(OtaStatus.FAILURE.name)
            return OtaStatus.FAILURE

    finalize_rollback = finalize_update

    def write_env(self, type: str, value):
        if type == "status":
            self._boot_control.store_standby_ota_status(value)
        elif type == "version":
            self._boot_control.store_standby_ota_version(value)


class MainECUAdapter(BootControlMixin):
    """
    load platform specific resources dynamically
    """

    def __init__(self):
        self._boot_control = ota_partition.OtaPartitionFile()
        self._ota_status = self.initialize_ota_status()

        self._mount_point = cfg.MOUNT_POINT
        self._passwd_file = cfg.PASSWD_FILE
        self._group_file = cfg.GROUP_FILE

        super().__init__()
