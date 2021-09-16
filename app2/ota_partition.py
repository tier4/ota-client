import os
import re
import subprocess
import shlex
import tempfile
import shutil
from pathlib import Path


class OtaPartition:
    """
    NOTE:
    device means: sda3
    device_file means: /dev/sda3
    """

    def __init__(self, boot_dir: Path, boot_ota_partition_file: Path):
        self._active_root_device_cache = None
        self._standby_root_device_cache = None
        self._boot_dir = boot_dir
        self._boot_ota_partition_file = boot_ota_partition_file

    def get_active_boot_device(self):
        """
        returns device linked from /boot/ota-partition
        e.g. if /boot/ota-partition links to ota-partition.sda3, sda3 is returned.
        NOTE: cache cannot be used since active and standby boot is switched.
        """
        # read link
        try:
            link = os.readlink(self._boot_dir / self._boot_ota_partition_file)
        except FileNotFoundError:
            # TODO:
            # backward compatibility
            # create boot ota-partition here?
            raise
        m = re.match(rf"{str(self._boot_ota_partition_file)}.(.*)", link)
        active_boot_device = m.group(1)
        return active_boot_device

    def get_standby_boot_device(self):
        """
        returns device not linked from /boot/ota-partition
        e.g. if /boot/ota-partition links to ota-partition.sda3, and
        sda3 and sda4 root device exist, sda4 is returned.
        NOTE: cache cannot be used since active and standby boot is switched.
        """
        active_root_device = self.get_active_root_device()
        standby_root_device = self.get_standby_root_device()

        active_boot_device = self.get_active_boot_device()
        if active_boot_device == active_root_device:
            return standby_root_device
        elif active_boot_device == standby_root_device:
            return active_root_device

        raise ValueError(
            f"illegal active_boot_device={active_boot_device}, "
            f"active_boot_device={active_root_device}, "
            f"standby_root_device={standby_root_device}"
        )

    def get_active_root_device(self):
        if self._active_root_device_cache:  # return cache if available
            return self._active_root_device_cache

        self._active_root_device_cache = self._get_root_device_file().lstrip("/dev")
        return self._active_root_device_cache

    def get_standby_root_device(self):
        """
        returns standby root device
        standby root device is:
        fstype ext4, sibling device of root device and not boot device.
        """
        if self._standby_root_device_cache:  # return cache if available
            return self._standby_root_device_cache

        # find root device
        root_device_file = self._get_root_device_file()

        # find boot device
        boot_device_file = self._get_boot_device_file()

        # find parent device from root device
        parent_device_file = self._get_parent_device_file(root_device_file)

        # find standby device file from root and boot device file
        self._standby_root_device_cache = self._get_standby_device_file(
            parent_device_file,
            root_device_file,
            boot_device_file,
        ).lstrip("/dev")
        return self._standby_root_device_cache

    """ utility functions """

    def store_active_ota_status(self, status):
        standby_boot = self.get_standby_boot_device()
        pass

    def update_boot_partition(self, boot_device):
        """
        updates /boot/ota-partition symbolink to /boot/ota-partition.{boot_device}
        e.g. /boot/ota-partition -> /boot/ota-partition.{boot_device}
        """
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            link = os.path.join(d, "templink")
            # create temporary link file to link `ota-partition.{boot_device}`.
            os.symlink(f"{str(self._boot_ota_partition_file)}.{boot_device}", link)
            # move created link to /boot/ota-partition
            os.rename(link, self._boot_dir / self._boot_ota_partition_file)

    """ private from here """

    def _findmnt_cmd(self, mount_point):
        cmd = f"findmnt -n -o SOURCE {mount_point}"
        return subprocess.check_output(shlex.split(cmd)).decode().strip()

    def _get_root_device_file(self):
        return self._findmnt_cmd("/")

    def _get_boot_device_file(self):
        return self._findmnt_cmd("/boot")

    def _get_parent_device_file(self, child_device_file):
        cmd = f"lsblk -ipn -o PKNAME {child_device_file}"
        return subprocess.check_output(shlex.split(cmd)).decode().strip()

    def _get_standby_device_file(
        self, parent_device_file, root_device_file, boot_device_file
    ):
        # list children device file from parent device
        cmd = f"lsblk -Pp -o NAME,FSTYPE {parent_device_file}"
        output = subprocess.check_output(shlex.split(cmd)).decode()
        # FSTYPE="ext4" and
        # not (parent_device_file, root_device_file and boot_device_file)
        for blk in output.split("\n"):
            m = re.match(r'NAME="(.*)" FSTYPE="(.*)"', blk)
            if (
                m.group(1) != parent_device_file
                and m.group(1) != root_device_file
                and m.group(1) != boot_device_file
                and m.group(2) == "ext4"
            ):
                return m.group(1)
        raise ValueError(f"lsblk output={output} is illegal")


class OtaPartitionFile(OtaPartition):
    def __init__(self, boot_dir: Path, boot_ota_partition_file: Path):
        super().__init__(boot_dir, boot_ota_partition_file)

    def get_standby_boot_partition_path(self):
        device = self.get_standby_boot_device()
        path = self._boot_dir / self._boot_ota_partition_file.with_suffix(f".{device}")
        return path

    def store_active_ota_status(self, status):
        """
        NOTE:
        In most cases of saving a status to active ota status, the status is a
        `success`.
        """
        device = self.get_active_boot_device()
        path = self._boot_dir / self._boot_ota_partition_file.with_suffix(f".{device}")
        self._store_string(path / "status", status)

    def store_standby_ota_status(self, status: str):
        device = self.get_standby_boot_device()
        path = self._boot_dir / self._boot_ota_partition_file.with_suffix(f".{device}")
        self._store_string(path / "status", status)

    def store_standby_ota_version(self, version: str):
        device = self.get_standby_boot_device()
        path = self._boot_dir / self._boot_ota_partition_file.with_suffix(f".{device}")
        self._store_string(path / "version", version)

    def load_ota_status(self):
        device = self.get_standby_boot_device()
        path = self._boot_dir / self._boot_ota_partition_file.with_suffix(f".{device}")
        return self._load_string(path / "status")

    def load_ota_version(self):
        device = self.get_standby_boot_device()
        path = self._boot_dir / self._boot_ota_partition_file.with_suffix(f".{device}")
        return self._load_string(path / "version")

    def cleanup_standby_boot_partition(self):
        """
        removes standby boot partition other than "status" and "version"
        """
        device = self.get_standby_boot_device()
        path = self._boot_dir / self._boot_ota_partition_file.with_suffix(f".{device}")
        removes = [
            f for f in path.glob("*") if f.name != "status" and f.name != "version"
        ]
        for f in removes:
            if f.is_dir():
                shutil.rmtree(str(f))
            else:
                f.unlink()

    def create_standby_boot_kernel_files(self):
        device = self.get_standby_boot_device()
        standby_path = self._boot_ota_partition_file.with_suffix(f".{device}")
        path = self._boot_dir / standby_path

        # find vmlinuz-* under /boot/ota-partition.{standby}
        vmlinuz_list = list(path.glob("vmlinuz-*"))
        if len(vmlinuz_list) != 1:
            raise ValueError(f"unintended vmlinuz list={vmlinuz_list}")
        # create symbolic link vmlinuz-ota -> vmlinuz-* under /boot/ota-partition.{standby}
        # NOTE: standby boot partition is cleaned-up when updating
        (path / "vmlinuz-ota").symlink_to(vmlinuz_list[0].name)

        # find initrd.img-* under /boot/ota-partition.{standby}
        initrd_img_list = list(path.glob("initrd.img-*"))
        if len(initrd_img_list) != 1:
            raise ValueError(f"unintended initrd.img list={initrd_img_list}")
        # create symbolic link initrd.img-ota -> initrd.img-* under /boot/ota-partition.{standby}
        # NOTE: standby boot partition is cleaned-up when updating
        (path / "initrd.img-ota").symlink_to(initrd_img_list[0].name)

        vmlinuz_file = "vmlinuz-ota.standby"
        initrd_img_file = "initrd.img-ota.standby"
        # create symbolic link vmlinuz-ota.standby -> ota-partition.{standby}/vmlinuz-ota under /boot
        (self._boot_dir / vmlinuz_file).unlink(missing_ok=True)
        (self._boot_dir / vmlinuz_file).symlink_to(standby_path / "vmlinuz-ota")
        # create symbolic link initrd.img-ota.standby -> ota-partition.{standby}/initrd.img-ota under /boot
        (self._boot_dir / initrd_img_file).unlink(missing_ok=True)
        (self._boot_dir / initrd_img_file).symlink_to(standby_path / "initrd.img-ota")
        return vmlinuz_file, initrd_img_file

    """ private functions from here """

    def _store_string(self, path, string):
        with tempfile.NamedTemporaryFile("w", delete=False, prefix=__name__) as f:
            f.write(string)
            shutil.move(f.name, path)

    def _load_string(self, path):
        try:
            with open(path) as f:
                return f.read()
        except FileNotFoundError as e:
            return ""
