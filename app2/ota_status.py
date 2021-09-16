from enum import Enum, unique
from ota_partition import OtaPartitionFile
from grub_control import GrubControl
from pathlib import Path
import subprocess
import shlex
import shutil


@unique
class OtaStatus(Enum):
    INITIALIZED = 0
    SUCCESS = 1
    FAILURE = 2
    UPDATING = 3
    ROLLBACKING = 4
    ROLLBACK_FAILURE = 5


class OtaStatusControl:
    BOOT_DIR = Path("/boot")
    BOOT_OTA_PARTITION_FILE = Path("ota-partition")

    def __init__(self):
        self._ota_partition = OtaPartitionFile(
            OtaStatusControl.BOOT_DIR, OtaStatusControl.BOOT_OTA_PARTITION_FILE
        )
        self._grub_control = GrubControl()
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

        self._ota_partition.store_standby_ota_status(OtaStatus.UPDATING.name)
        self._ota_partition.store_standby_ota_version(version)
        self._ota_partition.cleanup_standby_boot_partition()

        standby_root = self._ota_partition.get_standby_root_device()
        mount_path.mkdir(exist_ok=True)
        self._mount_and_clean(f"/dev/{standby_root}", mount_path)

    def leave_updating(self, mounted_path: Path):
        active_root_device = self._ota_partition.get_active_root_device()
        standby_root_device = self._ota_partition.get_standby_boot_device()
        self._grub_control.update_fstab(
            mounted_path, active_root_device, standby_root_device
        )
        (
            vmlinuz_file,
            initrd_img_file,
        ) = self._ota_partition.create_standby_boot_kernel_files()
        self._grub_control.create_custom_cfg_and_reboot(
            active_root_device, standby_root_device, vmlinuz_file, initrd_img_file
        )

    def enter_rollbacking(self):
        # check status
        if self.ota_status not in [
            OtaStatus.SUCCESS,
            OtaStatus.ROLLBACK_FAILURE,
        ]:
            raise ValueError(f"status={self.status} is illegal for rollback")

        standby_boot = self._ota_partition.get_standby_boot_device()
        standby_status_path = f"/boot/ota-partition.{standby_boot}/status"
        self._store_ota_status(standby_status_path, OtaStatus.ROLLBACKING.name)
        self._ota_status = OtaStatus.ROLLBACKING

    def leave_rollbacking(self):
        standby_boot = self._ota_partition.get_standby_boot_device()
        self._grub_control.create_custom_cfg_and_reboot()

    """ private functions from here """

    def _initialize_ota_status(self):
        # TODO:
        status_string = self._ota_partition.load_ota_status()
        ota_status = (
            OtaStatus.INITIALIZED if status_string == "" else OtaStatus[status_string]
        )
        return ota_status

    def _mount_and_clean(self, device_file, mount_point):
        try:
            self._mount_cmd(device_file, mount_point)
            self._clean_cmd(mount_point)
        except subprocess.CalledProcessError:
            # try again after umount
            self._umount_cmd(mount_point)
            self._mount_cmd(device_file, mount_point)
            self._clean_cmd(mount_point)

    def _mount_cmd(self, device_file, mount_point):
        cmd_mount = f"mount {device_file} {mount_point}"
        return subprocess.check_output(shlex.split(cmd_mount))

    def _umount_cmd(self, mount_point):
        cmd_umount = f"umount {mount_point}"
        return subprocess.check_output(shlex.split(cmd_umount))

    def _clean_cmd(self, mount_point):
        cmd_rm = f"rm -rf {mount_point}/"
        return subprocess.check_output(shlex.split(cmd_rm))
