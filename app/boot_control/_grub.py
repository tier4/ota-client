import re
import os
import subprocess
import shlex
import tempfile
import shutil
from pathlib import Path
from pprint import pformat

import log_util
from configs import config as cfg
from ota_error import OtaErrorUnrecoverable

assert cfg.BOOTLOADER == "grub"

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class GrubCfgParser:
    def __init__(self, grub_cfg):
        self._grub_cfg = grub_cfg

    def parse(self):
        menu, _ = self._parse(self._grub_cfg, False)
        logger.info(f"menu={pformat(menu)}")
        return menu

    def _parse(self, cfg, in_submenu):
        """
        returns [
          {}, # menuentry
          {}, # menuentry
          [ # submenu
            {}, # menuentry
            {}, # menuentry
          ],
          [ # submenu
            {}, # menuentry
          ],
          {}, # menuentry
        """
        pos = 0
        braces = []
        menus = []
        while True:
            m = re.search(r"(menuentry\s.*{|submenu\s.*{|})", cfg[pos:])
            if m:
                if m.group(1).startswith("menuentry"):
                    braces.append(pos + m.span()[0])
                pos += m.span()[1]
                if m.group(1).startswith("submenu"):
                    menu, sub_pos = self._parse(cfg[pos:], True)
                    pos += sub_pos
                    menus.append(menu)
                if m.group(1) == "}":
                    try:
                        begin = braces.pop()
                        # parse [begin:end]
                        linux = re.search(r"[ \t]*linux\s+/vmlinuz.*", cfg[begin:pos])
                        initrd = re.search(r"[ \t]*initrd\s+/initrd.*", cfg[begin:pos])
                        entry = {}
                        entry["entry"] = cfg[begin:pos]
                        entry["linux"] = None if linux is None else linux.group(0)
                        entry["initrd"] = None if initrd is None else initrd.group(0)
                        menus.append(entry)
                    except IndexError:
                        if in_submenu:
                            return menus, pos
                        else:
                            pass  # just ignore
            else:
                return menus, pos


class GrubControl:
    GRUB_CFG_FILE = cfg.GRUB_CFG_FILE  # Path("/boot/grub/grub.cfg")
    CUSTOM_CFG_FILE = cfg.CUSTOM_CFG_FILE  # Path("/boot/grub/custom.cfg")
    FSTAB_FILE = cfg.FSTAB_FILE  # Path("/etc/fstab")
    DEFAULT_GRUB_FILE = cfg.DEFAULT_GRUB_FILE  # Path("/etc/default/grub")

    def __init__(self):
        self._grub_cfg_file = Path(GrubControl.GRUB_CFG_FILE)
        self._custom_cfg_file = Path(GrubControl.CUSTOM_CFG_FILE)
        self._fstab_file = Path(GrubControl.FSTAB_FILE)
        self._default_grub_file = Path(GrubControl.DEFAULT_GRUB_FILE)

    def create_custom_cfg_and_reboot(
        self, standby_device, vmlinuz_file, initrd_img_file
    ):
        # pick up booted menuentry to create custom.cfg
        booted_menuentry = self._get_booted_menuentry()
        logger.info(f"{booted_menuentry=}")
        # modify booted menuentry for custom.cfg
        custom_cfg = self._update_menuentry(
            booted_menuentry, standby_device, vmlinuz_file, initrd_img_file
        )
        logger.info(f"{custom_cfg=}")
        # store custom.cfg
        with tempfile.NamedTemporaryFile("w", delete=False, prefix=__name__) as f:
            temp_name = f.name
            f.write(custom_cfg)
            f.flush()
            os.fsync(f.fileno())
        # should not be called within the NamedTemporaryFile context
        shutil.move(temp_name, self._custom_cfg_file)

        # grub-reboot
        self._grub_reboot()
        # reboot
        self.reboot()

    def update_grub_cfg(self, device, default_vmlinuz, grub_cfg_file):
        logger.info(f"device={device}, grub_cfg_file={grub_cfg_file}")
        """
        This function updates /etc/default/grub and and /boot/grub/grub.cfg to
        boot from device with default_vmlinuz kernel.
        """
        # 1. update /etc/default/grub with:
        #    GRUB_TIMEOUT_STYLE=menu
        #    GRUB_TIMEOUT=10
        #    GRUB_DISABLE_SUBMENU=y
        patterns = {
            "GRUB_TIMEOUT_STYLE=": "menu",
            "GRUB_TIMEOUT=": "10",
            "GRUB_DISABLE_SUBMENU=": "y",
        }
        self._update_default_grub(patterns)

        # 2. create temporary grub.cfg with grub-mkconfig
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            file_name = Path(d) / "grub.cfg"
            self._grub_mkconfig_cmd(file_name)

            # 3. count a number of default_vmlinuz entry from temporary grub.cfg
            menus = GrubCfgParser(open(file_name).read()).parse()
            uuid = self._get_uuid(device)
            number = self._count_menuentry(menus, uuid, default_vmlinuz)
            logger.info(f"{number=}")
            if number < 0:
                raise OtaErrorUnrecoverable(
                    f"menuentry not found: UUID={uuid}, vmlinuz={default_vmlinuz}, menus={menus}"
                )

        # 4. update /etc/default/grub with:
        #    GRUB_DEFAULT={number}
        patterns = {
            "GRUB_DEFAULT=": str(number),
        }
        self._update_default_grub(patterns)

        # 5. update /boot/grub/grub.cfg with grub-mkconfig
        self._grub_mkconfig_cmd(grub_cfg_file)

    def reboot(self):
        cmd = "reboot"
        return subprocess.check_output(shlex.split(cmd))

    def update_fstab(self, mount_point, active_device, standby_device):
        logger.info(f"{mount_point=},{active_device=},{standby_device=}")
        """
        NOTE:
        fstab operation might not be a part of grub, but uuid operation is only
        done in this class, so this function is implemented in this class.
        """
        active_uuid = self._get_uuid(active_device)
        standby_uuid = self._get_uuid(standby_device)
        logger.info(f"{active_uuid=},{standby_uuid=}")

        fstab_active = open(self._fstab_file).readlines()  # active partition fstab

        # standby partition fstab (to be merged)
        fstab_standby = open(mount_point / "etc" / "fstab").readlines()

        fstab_standby_dict = {}
        for line in fstab_standby:
            if not line.startswith("#") and not line.startswith("\n"):
                path = line.split()[1]
                fstab_standby_dict[path] = line
        logger.info(f"fstab_standby_dict={pformat(fstab_standby_dict)}")

        # merge entries
        merged = []
        for line in fstab_active:
            if line.startswith("#") or line.startswith("\n"):
                merged.append(line)
            else:
                path = line.split()[1]
                if path in fstab_standby_dict.keys():
                    merged.append(fstab_standby_dict[path])
                    del fstab_standby_dict[path]
                else:
                    merged.append(line.replace(active_uuid, standby_uuid))
        for v in fstab_standby_dict.values():
            merged.append(v)

        logger.info(f"{merged=}")
        with open(mount_point / "etc" / "fstab", "w") as f:
            f.writelines(merged)
            f.flush()
            os.fsync(f.fileno())

    def get_booted_vmlinuz_and_uuid(self):
        cmdline = self._get_cmdline()
        m = re.match(r"BOOT_IMAGE=/(\S*)\s*root=UUID=(\S*)", cmdline)
        vmlinuz = m.group(1)
        uuid = m.group(2)
        return vmlinuz, uuid

    """ private from here """

    def _find_menuentry(self, menus, uuid, vmlinuz):
        # NOTE: Only UUID sepcifier is supported.
        for menu in menus:
            if type(menu) is dict:
                m = re.match(r"\s*linux\s+/(\S*)\s+root=UUID=(\S+)\s*", menu["linux"])
                if m and m.group(1) == vmlinuz and m.group(2) == uuid:
                    return menu
            elif type(menu) is list:
                ret = self._find_menuentry(menu, uuid, vmlinuz)
                if ret is not None:
                    return ret
        return None

    def _count_menuentry(self, menus, uuid, vmlinuz):
        # NOTE: Only UUID sepcifier is supported.
        for i, menu in enumerate(menus):
            if type(menu) is dict:
                m = re.match(r"\s*linux\s+/(\S*)\s+root=UUID=(\S+)\s*", menu["linux"])
                if m and m.group(1) == vmlinuz and m.group(2) == uuid:
                    return i
            else:
                raise OtaErrorUnrecoverable("GRUB_DISABLE_SUBMENU=y should be set")
        return -1

    def _get_booted_menuentry(self):
        """
        find grub.cfg menuentry from uuid and BOOT_IMAGE specified by /proc/cmdline
        """
        # grub.cfg
        grub_cfg = open(self._grub_cfg_file).read()
        menus = GrubCfgParser(grub_cfg).parse()

        # booted vmlinuz and initrd.img
        vmlinuz, uuid = self.get_booted_vmlinuz_and_uuid()
        logger.info(f"{vmlinuz=},{uuid=}")
        menuentry = self._find_menuentry(menus, uuid, vmlinuz)
        logger.info(f"{menuentry=}")
        return f"{menuentry['entry']}\n"  # append newline

    def _update_menuentry(self, menuentry, standby_device, vmlinuz, initrd_img):
        uuid = self._get_uuid(standby_device)
        logger.info(f"{uuid=}")
        # NOTE: Only UUID sepcifier is supported.
        replaced = re.sub(
            r"(.*\slinux\s+/)\S*(\s+root=UUID=)\S*(\s.*)",
            rf"\g<1>{vmlinuz}\g<2>{uuid}\g<3>",  # NOTE: use \g<1> instead of \1
            menuentry,
        )
        replaced = re.sub(
            r"(.*\sinitrd\s+/)\S*(\s.*)",
            rf"\g<1>{initrd_img}\g<2>",
            replaced,
        )
        logger.info(f"{replaced=}")
        return replaced

    def _update_default_grub(self, patterns):
        logger.info(f"{patterns=}")
        lines = open(self._default_grub_file).readlines()
        patterns_found = {}
        updated = []
        for line in lines:
            found = False
            for k, v in patterns.items():
                m = re.match(rf"{k}.*", line)
                if m:
                    found = True
                    updated.append(f"{k}{v}\n")
                    patterns_found[k] = v
                    break
            if not found:
                updated.append(line)

        logger.info(f"{updated=}")
        deltas = dict(patterns.items() - patterns_found.items())
        logger.info(f"{deltas=}")
        for k, v in deltas.items():
            updated.append(f"{k}{v}\n")

        with open(self._default_grub_file, "w") as f:
            f.writelines(updated)
            f.flush()
            os.fsync(f.fileno())

    def _grub_reboot_cmd(self, num):
        cmd = "sync"
        subprocess.check_output(shlex.split(cmd))
        cmd = f"grub-reboot {num}"
        return subprocess.check_output(shlex.split(cmd))

    def _grub_reboot(self):
        # count grub menu entry number
        grub_cfg = open(self._grub_cfg_file).read()
        menus = GrubCfgParser(grub_cfg).parse()
        self._grub_reboot_cmd(len(menus))

    def _get_uuid(self, device):
        cmd = f"lsblk -in -o UUID /dev/{device}"
        return subprocess.check_output(shlex.split(cmd)).decode().strip()

    def _get_cmdline(self):
        return open("/proc/cmdline").read()

    def _grub_mkconfig_cmd(self, outfile):
        cmd = f"grub-mkconfig -o {outfile}"
        subprocess.check_output(shlex.split(cmd))
        cmd = f"sync {outfile}"
        subprocess.check_output(shlex.split(cmd))


class _OtaPartition:
    """
    NOTE:
    device means: sda3
    device_file means: /dev/sda3
    """

    BOOT_DIR = cfg.BOOT_DIR  # Path("/boot")
    BOOT_OTA_PARTITION_FILE = cfg.BOOT_OTA_PARTITION_FILE  # Path("ota-partition")

    def __init__(self):
        self._active_root_device_cache = None
        self._standby_root_device_cache = None
        self._boot_dir = Path(_OtaPartition.BOOT_DIR)
        self._boot_ota_partition_file = Path(_OtaPartition.BOOT_OTA_PARTITION_FILE)

    def get_active_boot_device(self):
        """
        returns device linked from /boot/ota-partition
        e.g. if /boot/ota-partition links to ota-partition.sda3, sda3 is returned.
        NOTE: cache cannot be used since active and standby boot is switched.
        """
        # read link
        link = os.readlink(self._boot_dir / self._boot_ota_partition_file)
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

        raise OtaErrorUnrecoverable(
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
        raise OtaErrorUnrecoverable(f"lsblk output={output} is illegal")


class OtaPartitionFile(_OtaPartition):
    def __init__(self):
        super().__init__()
        self._grub_control = GrubControl()
        self._initialize_boot_partition()

    def get_standby_boot_partition_path(self):
        device = self.get_standby_boot_device()
        path = self._boot_dir / self._boot_ota_partition_file.with_suffix(f".{device}")
        return path

    def is_switching_boot_partition_from_active_to_standby(self):
        standby_boot_device = self.get_standby_boot_device()
        active_root_device = self.get_active_root_device()
        logger.info(f"{standby_boot_device=},{active_root_device=}")
        return standby_boot_device == active_root_device

    def switch_boot_partition_from_active_to_standby(self):
        standby_device = self.get_standby_boot_device()
        logger.info(f"{standby_device=}")
        # to update the link atomically, move is used.
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            temp_file = Path(d) / self._boot_ota_partition_file
            temp_file.symlink_to(
                self._boot_ota_partition_file.with_suffix(f".{standby_device}")
            )
            ota_partition_file = self._boot_dir / self._boot_ota_partition_file
            logger.info(f"switching: {os.readlink(temp_file)=} -> {ota_partition_file}")
            self._move_atomic(temp_file, ota_partition_file)
            logger.info(f"switched: {os.readlink(ota_partition_file)=}")

    def store_active_ota_status(self, status):
        """
        NOTE:
        In most cases of saving a status to active ota status, the status is a
        `success`.
        """
        device = self.get_active_boot_device()
        path = self._boot_dir / self._boot_ota_partition_file.with_suffix(f".{device}")
        logger.info(f"{device=},{path=},{status=}")
        self._store_string(path / "status", status)

    def store_standby_ota_status(self, status: str):
        device = self.get_standby_boot_device()
        path = self._boot_dir / self._boot_ota_partition_file.with_suffix(f".{device}")
        logger.info(f"{device=},{path=},{status=}")
        self._store_string(path / "status", status)

    def store_standby_ota_version(self, version: str):
        device = self.get_standby_boot_device()
        path = self._boot_dir / self._boot_ota_partition_file.with_suffix(f".{device}")
        self._store_string(path / "version", version)

    def load_ota_status(self):
        """NOTE: always load ota status from standby boot"""
        device = self.get_standby_boot_device()
        path = self._boot_dir / self._boot_ota_partition_file.with_suffix(f".{device}")
        return self._load_string(path / "status")

    def load_ota_version(self):
        device = self.get_active_boot_device()
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
        logger.info(f"{active_root_device=},{standby_root_device=}")
        self._grub_control.update_fstab(
            mount_path, active_root_device, standby_root_device
        )

    def create_custom_cfg_and_reboot(self, rollback=False):
        standby_root_device = self.get_standby_boot_device()
        vmlinuz_file, initrd_img_file = self._create_standby_boot_kernel_files(rollback)
        logger.info(f"{standby_root_device=},{vmlinuz_file=},{initrd_img_file=}")
        self._grub_control.create_custom_cfg_and_reboot(
            standby_root_device, vmlinuz_file, initrd_img_file
        )

    def update_grub_cfg(self):
        active_device = self.get_active_root_device()
        standby_boot_path = self.get_standby_boot_partition_path()
        logger.info(f"{active_device=},{standby_boot_path=}")
        self._grub_control.update_grub_cfg(
            active_device, "vmlinuz-ota", standby_boot_path / "grub.cfg"
        )

    """ private functions from here """

    def _create_standby_boot_kernel_files(self, rollback=False):
        device = self.get_standby_boot_device()
        standby_path = self._boot_ota_partition_file.with_suffix(f".{device}")
        path = self._boot_dir / standby_path

        if rollback:
            # read vmlinuz-ota link
            link = os.readlink(path / "vmlinuz-ota")
            link_path = Path(path / link)
            if not link_path.is_file() or link_path.is_symlink():
                raise OtaErrorUnrecoverable(f"unintended vmlinuz-ota link {link_path}")
            # read intrd.img-ota link
            link = os.readlink(path / "initrd.img-ota")
            link_path = Path(path / link)
            if not link_path.is_file() or link_path.is_symlink():
                raise OtaErrorUnrecoverable(
                    f"unintended initrd.img-ota link {link_path}"
                )
        else:
            # find vmlinuz-* under /boot/ota-partition.{standby}
            vmlinuz_list = list(path.glob("vmlinuz-*"))
            if len(vmlinuz_list) != 1:
                raise OtaErrorUnrecoverable(f"unintended vmlinuz list={vmlinuz_list}")
            # create symbolic link vmlinuz-ota -> vmlinuz-* under /boot/ota-partition.{standby}
            # NOTE: standby boot partition is cleaned-up when updating
            (path / "vmlinuz-ota").symlink_to(vmlinuz_list[0].name)

            # find initrd.img-* under /boot/ota-partition.{standby}
            initrd_img_list = list(path.glob("initrd.img-*"))
            if len(initrd_img_list) != 1:
                raise OtaErrorUnrecoverable(
                    f"unintended initrd.img list={initrd_img_list}"
                )
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
            f.flush()
            os.fsync(f.fileno())
        # should not be called within the NamedTemporaryFile context
        shutil.move(temp_name, path)

    def _load_string(self, path):
        try:
            with open(path) as f:
                return f.read().strip()
        except FileNotFoundError:
            return ""

    def _initialize_boot_partition(self):
        """
        NOTE:
        get_active_boot_device and get_standby_boot_device may not be used
        since boot partitions are not created yet in some case.
        """
        boot_ota_partition = self._boot_dir / self._boot_ota_partition_file

        try:
            active_device = self.get_active_boot_device()
            standby_device = self.get_standby_boot_device()
        except FileNotFoundError:
            active_device = self.get_active_root_device()
            standby_device = self.get_standby_root_device()
        logger.info(f"{active_device=},{standby_device=}")

        active_boot_path = boot_ota_partition.with_suffix(f".{active_device}")
        standby_boot_path = boot_ota_partition.with_suffix(f".{standby_device}")
        logger.info(f"{active_boot_path=},{standby_boot_path=}")

        if standby_boot_path.is_dir() and (standby_boot_path / "status").is_file():
            # already initialized
            logger.info("already initialized")
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
                logger.error(f"unintended file type: path={path}")
                raise OtaErrorUnrecoverable(f"unintended file type: path={path}")

        vmlinuz, _ = self._grub_control.get_booted_vmlinuz_and_uuid()
        logger.info(f"{vmlinuz=}")
        m = re.match(r"vmlinuz-(.*)", vmlinuz)
        version = m.group(1)
        logger.info(f"{version=}")
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

        grub_cfg_file = self._grub_control._grub_cfg_file
        if not grub_cfg_file.is_symlink():
            ota_partition_dir = self._boot_dir / "ota-partition"
            # copy grub.cfg under ota-partition
            shutil.copy2(grub_cfg_file, ota_partition_dir)
            with tempfile.TemporaryDirectory(prefix=__name__) as d:
                # create temp symlink
                temp_file = Path(d) / "temp_link"
                temp_file.symlink_to(Path("..") / "ota-partition" / "grub.cfg")
                # move temp symlink to grub.cfg
                self._move_atomic(temp_file, grub_cfg_file)

        # update grub.cfg
        self._grub_control.update_grub_cfg(
            active_device, "vmlinuz-ota", active_boot_path / "grub.cfg"
        )

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

    def _move_atomic(self, src: Path, dst: Path):
        cmd_mv = f"mv -T {str(src)} {str(dst)}"
        subprocess.check_output(shlex.split(cmd_mv))
        self._fsync(src.parent)
        self._fsync(dst.parent)
        # NOTE:
        # If the system is reset here, grub has a problem of not starting with
        # the proper partition, so the sync below is required.
        os.sync()

    def _fsync(self, path):
        try:
            fd = None
            fd = os.open(path, os.O_RDONLY)
            os.fsync(fd)
        finally:
            if fd:
                os.close(fd)
