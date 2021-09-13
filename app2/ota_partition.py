import os
import re
import subprocess
import shlex
import tempfile
import shutil


class OtaPartition:
    """
    NOTE:
    device means: sda3
    device_file means: /dev/sda3
    """

    OTA_PARTITION_FILE = "ota-partition"
    BOOT_OTA_PARTITION_FILE = f"/boot/{OTA_PARTITION_FILE}"

    def __init__(self):
        self._active_boot_device_cache = None
        self._standby_boot_device_cache = None
        self._active_root_device_cache = None
        self._standby_root_device_cache = None

    def get_active_boot_device(self):
        if self._active_boot_device_cache:  # return cache if available
            return self._active_boot_device_cache
        # read link
        try:
            link = os.readlink(OtaPartition.BOOT_OTA_PARTITION_FILE)
        except FileNotFoundError:
            # TODO: backward compatibility
            return None
        m = re.match(r"ota-partition.(.*)", link)
        self._active_boot_device_cache = m.group(1)
        return self._active_boot_device_cache

    def get_standby_boot_device(self):
        if self._standby_boot_device_cache:  # return cache if available
            return self._standby_boot_device_cache
        active_root_device = self.get_active_root_device()
        standby_root_device = self.get_standby_root_device()

        active_boot_device = self.get_active_boot_device()
        if active_boot_device == active_root_device:
            standby_boot_device = standby_root_device
        elif active_boot_device == standby_root_device:
            standby_boot_device = active_root_device
        else:
            raise ValueError(
                f"illegal active_boot_device={active_boot_device}, "
                f"active_boot_device={active_root_device}, "
                f"standby_root_device={standby_root_device}"
            )
        self._standby_boot_device_cache = standby_boot_device
        return self._standby_boot_device_cache

    def get_active_root_device(self):
        if self._active_root_device_cache:  # return cache if available
            return self._active_root_device_cache

        self._active_root_device_cache = self._get_root_device_file().lstrip("/dev")
        return self._active_root_device_cache

    def get_standby_root_device(self):
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

    def update_boot_partition(self, boot_device):
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            link = os.path.join(d, "templink")
            # create link file to link /boot/ota-partition.{boot_device}
            os.symlink(f"ota-partition.{boot_device}", link)
            # move link created to /boot/ota-partition
            os.rename(link, OtaPartition.BOOT_OTA_PARTITION_FILE)

    def update_fstab_root_partition(
        self, standby_device, src_fstab_file, dst_fstab_file
    ):
        fstab = open(src_fstab_file).readlines()

        updated_fstab = []
        for line in fstab:
            if line.startswith("#"):
                updated_fstab.append(line)
                continue
            line_split = line.split()
            if line_split[1] == "/":
                # TODO
                # replace UUID=... or device file
                # if line_split[0].find("UUID="):
                #     line.replace()
                # elif line_split[0].find(device):
                #     line.replace()
                # ...
                pass
            else:
                updated_fstab.append(line)

        with tempfile.NamedTemporaryFile("w", delete=False, prefix=__name__) as f:
            temp_name = f.name
            f.writelines(updated_fstab)
            shutil.move(temp_name, dst_fstab_file)

    """ private from here """

    def _findmnt_cmd(self, mount_point):
        cmd = f"findmnt -n -o SOURCE {mount_point}"
        return subprocess.check_output(shlex.split(cmd))

    def _get_root_device_file(self):
        return self._findmnt_cmd("/").decode().strip()

    def _get_boot_device_file(self):
        return self._findmnt_cmd("/boot").decode().strip()

    def _get_parent_device_file(self, child_device_file):
        cmd = f"lsblk -ipno PKNAME {device_file}"
        return subprocess.check_output(shlex.split(cmd))

    def _get_standby_device_file(
        self, parent_device_file, root_device_file, boot_device_file
    ):
        # list children device file with parent
        cmd = f"lsblk -Pp -o NAME,FSTYPE {parent_device_file}"
        output = subprocess.check_output(shlex.split(cmd))
        # FSTYPE="ext4" and
        # not (parent_device_file, root_device_file and boot device_file)
        for blk in output.decode().split("\n"):
            m = re.match(r'NAME="(.*)" FSTYPE="(.*)"', blk)
            if (
                m.group(1) != parent_device_file
                and m.group(1) != root_device_file
                and m.group(1) != boot_device_file
                and m.group(2) == "ext4"
            ):
                return m.group(1)
        raise ValueError(f"lsblk output={output} is illegal")
