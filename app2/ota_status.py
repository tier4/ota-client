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

    def enter_updating(self, version, mount_path):
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
        self._mount_cmd(f"/dev/{standby_root}", mount_path)
        shutil.rmtree(mount_path)

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
        active_boot = self._ota_partition.get_active_boot_device()
        standby_boot = self._ota_partition.get_standby_boot_device()
        active_root = self._ota_partition.get_active_root_device()
        standby_root = self._ota_partition.get_standby_root_device()

        if active_boot != active_root:
            self._ota_partition.update_boot_partition(active_root)
            self._grub_control.reboot()

        active_status_path = f"/boot/ota-partition.{active_boot}/status"
        standby_status_path = f"/boot/ota-partition.{standby_boot}/status"

        active_status = self._load_ota_status(active_status_path)
        standby_status = self._load_ota_status(standby_status_path)
        if (
            active_status == OtaStatus.INITIALIZED
            and standby_status == OtaStatus.INITIALIZED
        ):
            return OtaStatus.INITIALIZED
        if standby_status == OtaStatus.UPDATING:
            # standby status is updating w/o switching partition and (re)booted.
            return OtaStatus.FAILURE
        if standby_status == OtaStatus.ROLLBACKING:
            # standby status is rollbacking w/o switching partition and (re)booted.
            return OtaStatus.ROLLBACK_FAILURE
        if active_status == OtaStatus.UPDATING:
            self._grub_control.update_grub_cfg()
            self._store_ota_status(OtaStatus.SUCCESS.name)
            return OtaStatus.SUCCESS
        return OtaStatus[active_status]

    def _load_ota_status(self, path):
        try:
            with open(path) as f:
                status = f.read().strip()  # if it contains whitespace
                if status in [s.name for s in OtaStatus]:
                    return OtaStatus[status]
                raise ValueError(f"{path}: status={status} is illegal")
        except FileNotFoundError as e:
            return OtaStatus.INITIALIZED

    def _load_ota_version(self, path):
        try:
            with open(path) as f:
                version = f.read().strip()  # if it contains whitespace
                return version
        except FileNotFoundError as e:
            return ""

    def _mount_cmd(self, device_file, mount_point):
        try:
            cmd_mount = f"mount {device_file} {mount_point}"
            return subprocess.check_output(shlex.split(cmd_mount))
        except subprocess.CalledProcessError:
            # try again after umount
            cmd_umount = f"umount {mount_point}"
            subprocess.check_output(shlex.split(cmd_umount))
            return subprocess.check_output(shlex.split(cmd_mount))

    def _umount_cmd(self, mount_point):
        cmd_umount = f"umount {mount_point}"
        return subprocess.check_output(shlex.split(cmd_umount))
