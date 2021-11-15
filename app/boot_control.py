from abc import ABC
from pathlib import Path


class BootControlMixinInterface(ABC):
    """
    platform neutral boot control interface
    """

    _boot_control = None
    _ota_status = None
    _mount_point = None

    def initialize_ota_status(self):
        pass

    def write_standby_ota_status(self, status):
        pass

    def write_initialized_ota_status(self):
        pass

    def write_ota_status(self, status):
        pass

    def load_ota_status(self):
        pass

    def get_standby_boot_partition_path(self) -> Path:
        pass

    def get_version(self):
        pass

    def boot_ctrl_pre_update(self, version):
        pass

    def boot_ctrl_post_update(self):
        pass

    def boot_ctrl_pre_rollback(self):
        pass

    def boot_ctrl_post_rollback(self):
        pass

    def finalize_update(self):
        pass

    finalize_rollback = finalize_update
