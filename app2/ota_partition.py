import os
import re
import subprocess
import shlex
import tempfile
import shutil
from pathlib import Path
from grub_control import GrubControl


class OtaPartition:
    """
    NOTE:
    device means: sda3
    device_file means: /dev/sda3
    """

    BOOT_DIR = Path("/boot")
    BOOT_OTA_PARTITION_FILE = Path("ota-partition")

    def __init__(self):
        self._active_root_device_cache = None
        self._standby_root_device_cache = None
        self._boot_dir = OtaPartition.BOOT_DIR
        self._boot_ota_partition_file = OtaPartition.BOOT_OTA_PARTITION_FILE

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
    def __init__(self):
        super().__init__()
        self._grub_control = GrubControl()
        self._initialize_boot_partition()

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

    def mount_standby_root_partition_and_clean(self, mount_path: Path):
        standby_root = self.get_standby_root_device()
        mount_path.mkdir(exist_ok=True)
        self._mount_and_clean(f"/dev/{standby_root}", mount_path)

    def update_fstab(self, mount_path: Path):
        active_root_device = self.get_active_root_device()
        standby_root_device = self.get_standby_boot_device()
        self._grub_control.update_fstab(
            mount_path, active_root_device, standby_root_device
        )

    def create_custom_cfg_and_reboot(self):
        active_root_device = self.get_active_root_device()
        standby_root_device = self.get_standby_boot_device()
        vmlinuz_file, initrd_img_file = self._create_standby_boot_kernel_files()
        self._grub_control.create_custom_cfg_and_reboot(
            active_root_device, standby_root_device, vmlinuz_file, initrd_img_file
        )

    """ private functions from here """

    def _create_standby_boot_kernel_files(self):
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

    def _store_string(self, path, string):
        with tempfile.NamedTemporaryFile("w", delete=False, prefix=__name__) as f:
            temp_name = f.name
            f.write(string)
        # should not be called within the NamedTemporaryFile context
        shutil.move(temp_name, path)

    def _load_string(self, path):
        try:
            with open(path) as f:
                return f.read()
        except FileNotFoundError as e:
            return ""

    def _initialize_boot_partition(self):
        """
        NOTE:
        In this function, get_active_boot_device and get_standby_boot_device
        can not be used since boot partitions are not created yet.
        """
        boot_ota_partition = self._boot_dir / self._boot_ota_partition_file

        active_device = self.get_active_root_device()
        standby_device = self.get_standby_root_device()
        active_boot_path = boot_ota_partition.with_suffix(f".{active_device}")
        standby_boot_path = boot_ota_partition.with_suffix(f".{standby_device}")

        if standby_boot_path.is_dir() and (standby_boot_path / "status").is_file():
            # already initialized
            return

        # create active boot partition
        active_boot_path.mkdir(exist_ok=True)

        # create standby boot partition
        standby_boot_path.mkdir(exist_ok=True)

        # copy regular file of vmlinuz-{version} initrd.img-{version}
        # config-{version} System.map-{version} to active_boot_path.
        # version is retrieved from /proc/cmdline.
        def _check_is_regular(path):
            if not path.is_file() or path.is_symlink():
                raise ValueError(f"unintended file type: path={path}")

        vmlinuz, _ = self._grub_control.get_booted_vmlinuz_and_uuid()
        m = re.match(r"vmlinuz-(.*)", vmlinuz)
        version = m.group(1)
        kernel_files = ("vmlinuz-", "initrd.img-", "config-", "System.map-")
        for kernel_file in kernel_files:
            path = self._boot_dir / f"{kernel_file}{version}"
            _check_is_regular(path)
            shutil.copy2(path, active_boot_path)

        # create symlink vmlinuz-ota -> vmlinuz-{version} under
        # /boot/ota-partition.{active}
        (active_boot_path / "vmlinuz-ota").unlink(missing_ok=True)
        (active_boot_path / "vmlinuz-ota").symlink_to(f"vmlinuz-{version}")

        # create symlink initrd.img-ota -> initrd.img-{version} under
        # /boot/ota-partition.{active}
        (active_boot_path / "initrd.img-ota").unlink(missing_ok=True)
        (active_boot_path / "initrd.img-ota").symlink_to(f"initrd.img-{version}")

        # create symlink ota-partition -> ota-partition.{active} under /boot
        (self._boot_dir / "ota-partition").unlink(missing_ok=True)
        (self._boot_dir / "ota-partition").symlink_to(
            self._boot_ota_partition_file.with_suffix(f".{active_device}")
        )
        # create symlink vmlinuz-ota -> ota-partition/vmlinuz-ota under /boot
        (self._boot_dir / "vmlinuz-ota").unlink(missing_ok=True)
        (self._boot_dir / "vmlinuz-ota").symlink_to(
            self._boot_ota_partition_file / "vmlinuz-ota"
        )
        # create symlink initrd.img-ota -> ota-partition/initrd.img-ota under /boot
        (self._boot_dir / "initrd.img-ota").unlink(missing_ok=True)
        (self._boot_dir / "initrd.img-ota").symlink_to(
            self._boot_ota_partition_file / "initrd.img-ota"
        )

        # update grub.cfg
        self._grub_control.update_grub_cfg(active_device, "vmlinuz-ota")

        # rm kernel_files
        # for kernel_file in kernel_files:
        #    path = self._boot_dir / f"{kernel_file}{version}"
        #    path.unlink()

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
        cmd_rm = f"rm -rf {mount_point}/*"
        return subprocess.check_output(cmd_rm, shell=True)  # to use `*`
