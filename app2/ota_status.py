from enum import Enum, unique
from ota_partition import OtaPartition
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
    def __init__(self):
        self._ota_partition = OtaPartition()
        self._grub_control = GrubControl()
        self._ota_status = self._get_initial_ota_status()
        self._fstab_file = "/etc/fstab"

    def get_boot_standby_path(self):
        standby_boot = self._ota_partition.get_standby_boot_device()
        return f"/boot/ota-partition.{standby_boot}/"

    def get_ota_status(self):
        return self._ota_status

    def enter_updating(self, version, mount_path):
        if self._ota_status not in [
            OtaStatus.INITIALIZED,
            OtaStatus.SUCCESS,
            OtaStatus.FAILURE,
            OtaStatus.ROLLBACK_FAILURE,
        ]:
            raise ValueError(f"status={self.status} is illegal for update")
        standby_boot = self._ota_partition.get_standby_boot_device()
        standby_status_path = f"/boot/ota-partition.{standby_boot}/status"
        standby_version_path = f"/boot/ota-partition.{standby_boot}/version"
        self._store_ota_status(standby_status_path, OtaStatus.UPDATING.name)
        self._store_ota_version(standby_version_path, version)
        self._ota_status = OtaStatus.UPDATING
        standby_boot_files_remove = [
            f
            for f in Path(f"/boot/ota-partition.{standby_boot}").glob("*")
            if f.name != "status" and f.name != "version"
        ]

        standby_root = self._ota_partition.get_standby_root_device()
        self._mount_cmd(f"/dev/{standby_root}", mount_path)
        shutil.rmtree(mount_path)
        for f in standby_boot_files_remove:
            if f.is_dir():
                shutil.rmtree(str(f))
            else:
                f.unlink()

    def leave_updating(self, mounted_path):
        # TODO: umount mounted_path
        active_boot = self._ota_partition.get_active_boot_device()
        standby_boot = self._ota_partition.get_standby_boot_device()
        self._ota_partition.update_fstab_root_partition(
            standby_boot,
            Path(self._fstab_file),
            Path(mounted_path) / Path(self._fstab_file).relative_to("/"),
        )
        self._ota_partition.update_boot_partition(standby_boot)
        self._grub_control.create_custom_cfg_and_reboot(active_boot, standby_boot)

    def enter_rollbacking(self):
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
        self._ota_partition.update_fstab_root_partition(standby_boot)
        self._ota_partition.update_boot_partition(standby_boot)
        self._grub_control.create_custom_cfg_and_reboot()

    def _get_initial_ota_status(self):
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

    def _store_ota_status(self, path, ota_status):
        # TODO:
        # create temp file
        # and write ota_status to it
        # and move to path
        pass

    def _load_ota_version(self, path):
        try:
            with open(path) as f:
                version = f.read().strip()  # if it contains whitespace
                return version
        except FileNotFoundError as e:
            return ""

    def _store_ota_version(self, path, version):
        # TODO:
        # create temp file
        # and write ota_status to it
        # and move to path
        pass

    def _mount_cmd(self, device_file, mount_point):
        try:
            cmd_mount = f"mount {device_file} {mount_point}"
            return subprocess.check_output(shlex.split(cmd_mount))
        except subprocess.CalledProcessError:
            # try again after umount
            cmd_umount = f"umount {mount_point}"
            subprocess.check_output(shlex.split(cmd_umount))
            return subprocess.check_output(shlex.split(cmd_mount))

    def _umount_cmd(mount_point):
        cmd_umount = f"umount {mount_point}"
        return subprocess.check_output(shlex.split(cmd_umount))
