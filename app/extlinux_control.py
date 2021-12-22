import io
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

import log_util
from configs import config as cfg
from ota_error import OtaErrorUnrecoverable
from ota_status import OtaStatus
from ota_client_interface import BootControlInterface

assert cfg.BOOTLOADER == "cboot"

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


def _read_file(path: Path) -> str:
    try:
        return path.read_text().strip()
    except:
        return ""


def _write_file(path: Path, input: str):
    path.write_text(input)


def _subprocess_call(cmd: str, *, raise_exception=False):
    try:
        logger.debug(f"cmd: {cmd}")
        subprocess.check_call(shlex.split(cmd), stdout=subprocess.DEVNULL)
    except subprocess.CalledProcessError as e:
        logger.warning(
            msg=f"command failed(exit-code: {e.returncode} stderr: {e.stderr} stdout: {e.stdout}): {cmd}"
        )
        if raise_exception:
            raise


def _subprocess_check_output(cmd: str, *, raise_exception=False) -> str:
    try:
        logger.debug(f"cmd: {cmd}")
        return subprocess.check_output(shlex.split(cmd)).decode().strip()
    except subprocess.CalledProcessError as e:
        logger.warning(
            msg=f"command failed(exit-code: {e.returncode} stderr: {e.stderr} stdout: {e.stdout}): {cmd}"
        )
        if raise_exception:
            raise
        return ""


class HelperFuncs:
    @staticmethod
    def _findfs(key: str, value: str) -> str:
        """
        findfs finds a partition by conditions
        Usage:
            findfs [options] {LABEL,UUID,PARTUUID,PARTLABEL}=<value>
        """
        _cmd = f"findfs {key}={value}"
        return _subprocess_check_output(_cmd)

    @staticmethod
    def _findmnt(p: str) -> str:
        _cmd = f"findmnt {p}"
        return _subprocess_check_output(_cmd)

    @staticmethod
    def _lsblk(args: str) -> str:
        _cmd = f"lsblk {args}"
        return _subprocess_check_output(_cmd)

    @staticmethod
    def _blkid(args: str) -> str:
        _cmd = f"blkid {args}"
        return _subprocess_check_output(_cmd)

    @classmethod
    def get_partuuid_by_dev(cls, dev: str) -> str:
        args = f"{dev} -s PARTUUID"
        res = cls._blkid(args)

        pa = re.compile(r'PARTUUID="?(?P<partuuid>[\w-]*)"?')
        v = pa.search(res).group("partuuid")

        return f"PARTUUID={v}"

    @classmethod
    def get_dev_by_partlabel(cls, partlabel: str) -> str:
        return cls._findfs("PARTLABEL", partlabel)

    @classmethod
    def get_dev_by_partuuid(cls, partuuid: str) -> str:
        return cls._findfs("PARTUUID", partuuid)

    @classmethod
    def get_rootfs_dev(cls) -> str:
        dev = Path(cls._findmnt("/ -o SOURCE -n")).resolve(strict=True)
        return str(dev)

    @classmethod
    def get_parent_dev(cls, child_device: str) -> str:
        """
        When `/dev/nvme0n1p1` is specified as child_device, /dev/nvme0n1 is returned.
        """
        cmd = f"-ipn -o PKNAME {child_device}"
        return cls._lsblk(cmd)

    @classmethod
    def get_family_devs(cls, parent_device: str) -> list:
        """
        When `/dev/nvme0n1` is specified as parent_device,
        ["NAME=/dev/nvme0n1", "NAME=/dev/nvme0n1p1", "NAME=/dev/nvme0n1p2"] is returned.
        """
        cmd = f"-Pp -o NAME {parent_device}"
        return cls._lsblk(cmd).splitlines()

    @classmethod
    def get_sibling_dev(cls, device: str) -> str:
        """
        When `/dev/nvme0n1p1` is specified as child_device, /dev/nvme0n1p2 is returned.
        """
        parent = cls.get_parent_dev(device)
        family = cls.get_family_devs(parent)
        partitions = {i.split("=")[-1].strip('"') for i in family[1:3]}
        res = partitions - {device}
        if len(res) == 1:
            (r,) = res
            return r
        raise OtaErrorUnrecoverable(
            f"device is has unexpected partition layout, {family=}"
        )


class Nvbootctrl:
    """
    NOTE: slot and rootfs are binding accordingly!
          partid mapping: p1->slot0, p2->slot1

    slot num: 0->A, 1->B
    """

    EMMC_DEV: str = "mmcblk0"
    NVME_DEV: str = "nvme0n1"
    # slot0<->slot1
    CURRENT_STANDBY_FLIP = {"0": "1", "1": "0"}
    # p1->slot0, p2->slot1
    PARTID_SLOTID_MAP = {"1": "0", "2": "1"}
    # slot0->p1, slot1->p2
    SLOTID_PARTID_MAP = {"0": "1", "1": "2"}

    # nvbootctrl
    @staticmethod
    def _nvbootctrl(arg: str, *, call_only=True, raise_exception=True) -> str:
        # NOTE: target is always set to rootfs
        _cmd = f"nvbootctrl -t rootfs {arg}"
        if call_only:
            _subprocess_call(_cmd, raise_exception=raise_exception)
        else:
            return _subprocess_check_output(
                _cmd, raise_exception=raise_exception
            ).strip()

    @classmethod
    def check_rootdev(cls, dev: str) -> bool:
        """
        check whether the givin dev is legal root dev or not

        NOTE: expect using UUID method to assign rootfs!
        """
        pa = re.compile(r"\broot=(?P<rdev>[\w=-]*)\b")
        ma = pa.search(_subprocess_check_output("cat /proc/cmdline")).group("rdev")

        if ma is None:
            raise OtaErrorUnrecoverable(
                f"failed to detect rootfs or PARTUUID method is not used"
            )
        uuid = ma.split("=")[-1]

        return Path(HelperFuncs.get_dev_by_partuuid(uuid)).resolve(strict=True) == Path(
            dev
        ).resolve(strict=True)

    @classmethod
    def get_current_slot(cls) -> str:
        return cls._nvbootctrl(f"get-current-slot", call_only=False)

    @classmethod
    def mark_boot_successful(cls, slot: str):
        cls._nvbootctrl(f"mark-boot-successful {slot}")

    @classmethod
    def set_active_boot_slot(cls, slot: str):
        cls._nvbootctrl(f"set-active-boot-slot {slot}")

    @classmethod
    def set_slot_as_unbootable(cls, slot: str):
        cls._nvbootctrl(f"set-slot-as-unbootable {slot}")

    @classmethod
    def is_slot_bootable(cls, slot: str) -> bool:
        try:
            cls._nvbootctrl(f"is-slot-bootable {slot}")
            return True
        except subprocess.CalledProcessError:
            return False

    @classmethod
    def is_slot_marked_successful(cls, slot: str) -> bool:
        try:
            cls._nvbootctrl(f"is-slot-marked-successful {slot}")
            return True
        except subprocess.CalledProcessError:
            return False


class ExtlinuxCfgFile:
    DEFAULT_HEADING = {
        "TIMEOUT": 30,
        "DEFAULT": "primary",
        "MENU TITLE": "L4T boot options",
    }

    def __init__(self):
        self._heading = self.DEFAULT_HEADING.copy()
        self._entry = dict()  # [name]entry

    def set_default_entry(self, label: str):
        self._heading["DEFAULT"] = label

    def load_entry(
        self,
        label: str,
        entry: dict = None,
        menu_lable: str = "",
        fdt: str = "",
        linux: str = "/boot/Image",
        initrd: str = "/boot/initrd",
        append: str = "",
    ):
        if entry:
            self._entry[label] = entry
        else:
            entry = dict()
            entry["MENU LABEL"] = menu_lable
            entry["LINUX"] = linux
            entry["INITRD"] = initrd
            entry["FDT"] = fdt
            entry["APPEND"] = append
            self._entry[label] = entry

    def load_extlinux_cfg_file(self, file: Path):
        """
        load entries from external extlinux conf file
        """
        if not file.is_file():
            return

        # search for entries
        in_entry = False
        entry_name, entry = "", dict()

        with open(file, "r") as f:
            for l in f.readlines():
                if l.strip().startswith("#"):
                    continue

                # top-level options
                if l.find("TIMEOUT") == 0:
                    self._heading["TIMEOUT"] = l.strip().split(" ")[-1]
                elif l.find("DEFAULT") == 0:
                    self._heading["DEFAULT"] = l.strip().replace("DEFAULT", "")
                elif l.find("MENU TITLE") == 0:
                    self._heading["MENU TITLE"] = l.strip().replace("MENU TITLE", "")

                # entry
                if in_entry:
                    if l.find("\t") != 0 or l.find(" ") != 0:
                        # we encount the end of an entry
                        in_entry = False
                        # save the previous entry
                        self.load_entry(label=entry_name, entry=entry)
                        # options that we don't know and are also not comments
                        if l:
                            entry[l.strip()] = ""
                    elif l.strip().find("LABEL") == 0:
                        # follow by another new entry
                        # save the previous entry
                        self.load_entry(label=entry_name, entry=entry)
                        # load new entry
                        entry_name, entry = l.strip().split(" ")[-1], dict()
                    # load entry's contents
                    elif l.find("LINUX") != -1:
                        entry["LINUX"] = l.strip().split(" ")[-1]
                    elif l.find("INITRD") != -1:
                        entry["INITRD"] = l.strip().split(" ")[-1]
                    elif l.find("FDT") != -1:
                        entry["FDT"] = l.strip().split(" ")[-1]
                    elif l.find("APPEND") != -1:
                        entry["APPEND"] = l.strip().replace("APPEND", "")
                    elif l.find("MENU LABEL") != -1:
                        entry["MENU LABEL"] = l.replace("MENU LABEL", "")
                elif l.strip().find("LABEL") == 0:
                    in_entry = True
                    entry_name, entry = l.strip().split(" ")[-1], dict()

    def get_entry(self, name: str) -> dict:
        """
        return the copy of specific entry
        """
        return self._entry.get(name, default=dict()).copy()

    def get_default_entry(self) -> dict:
        name = self._heading.get("DEFAULT")
        if name in self._entry:
            return self._entry.get(name).copy()
        else:
            return dict()

    def edit_entry(self, name: str, key: str, value: str):
        """
        edit specific entry
        """
        if name not in self._entry:
            return
        self._entry[name][key] = value

    def dump_cfg(self) -> str:
        """
        dump_cfg dumps the extlinux config file to a string
        """
        buff = io.StringIO()
        _write = lambda s="": buff.write(f"{s} \n")
        _comment = lambda s="": buff.write(f"# {s} \n")

        _comment("Auto generated by ota-client, DO NOT EDIT!")
        # generate heading
        for k, v in self._heading.items():
            _write(f"{k} {v}")

        # populate entry
        for t, e in self._entry.items():
            _write(f"LABEL {t}")
            for mt, mop in e.items():
                _write(f"\t{mt} {mop}")

        res = buff.getvalue()
        logger.debug("generated extlinux.conf: ")
        logger.debug(f"\n################ \n {res} \n ################\n")
        return res


class CBootControl:
    def __init__(self):
        self._linux = cfg.KERNEL
        self._initrd = cfg.INITRD
        self._fdt = cfg.FDT
        self._cmdline_extra = cfg.EXTRA_CMDLINE

        # NOTE: only support r580 platform right now!
        # detect the chip id
        tegra_chip_id_f = Path("/sys/module/tegra_fuse/parameters/tegra_chip_id")
        self.chip_id = _read_file(tegra_chip_id_f).strip()
        if self.chip_id is None or int(self.chip_id) not in cfg.CHIP_ID_MODEL_MAP:
            raise NotImplementedError(
                f"unsupported platform found (chip_id: {self.chip_id}), abort"
            )

        self.chip_id = int(self.chip_id)
        self.model = cfg.CHIP_ID_MODEL_MAP[self.chip_id]
        logger.info(f"{self.model=}, (chip_id={hex(self.chip_id)})")

        # initializing dev info
        self._init_dev_info()

    def _init_dev_info(self):
        self.current_slot: str = Nvbootctrl.get_current_slot()
        self.current_rootfs_dev: str = HelperFuncs.get_rootfs_dev()
        # NOTE: boot dev is always emmc device now
        self.current_boot_dev: str = f"/dev/{Nvbootctrl.EMMC_DEV}p{Nvbootctrl.SLOTID_PARTID_MAP[self.current_slot]}"

        self.standby_slot: str = Nvbootctrl.CURRENT_STANDBY_FLIP[self.current_slot]
        standby_partid = Nvbootctrl.SLOTID_PARTID_MAP[self.standby_slot]
        self.standby_boot_dev: str = f"/dev/{Nvbootctrl.EMMC_DEV}p{standby_partid}"

        # detect rootfs position
        if self.current_rootfs_dev.find(Nvbootctrl.NVME_DEV) != -1:
            logger.debug(f"rootfs on external storage deteced, nvme rootfs is enable")
            self.is_rootfs_on_external = True
            self.standby_rootfs_dev = f"/dev/{Nvbootctrl.NVME_DEV}p{standby_partid}"
            self.standby_slot_partuuid = HelperFuncs.get_partuuid_by_dev(
                self.standby_rootfs_dev
            )
        elif self.current_rootfs_dev.find(Nvbootctrl.EMMC_DEV) != -1:
            logger.debug(f"using internal storage as rootfs")
            self.is_rootfs_on_external = False
            self.standby_rootfs_dev = f"/dev/{Nvbootctrl.EMMC_DEV}p{standby_partid}"
            self.standby_slot_partuuid = HelperFuncs.get_partuuid_by_dev(
                self.standby_rootfs_dev
            )
        else:
            raise NotImplementedError(
                f"rootfs on {self.current_rootfs_dev} is not supported, abort"
            )

        # ensure rootfs is as expected
        if not Nvbootctrl.check_rootdev(self.current_rootfs_dev):
            msg = f"rootfs mismatch, expect {self.current_rootfs_dev} as rootfs"
            raise RuntimeError(msg)
        elif Nvbootctrl.check_rootdev(self.standby_rootfs_dev):
            msg = (
                f"rootfs mismatch, expect {self.standby_rootfs_dev} as standby slot dev"
            )
            raise RuntimeError(msg)

        logger.info(f"dev info initializing completed")
        logger.info(
            f"{self.current_slot=}, {self.current_boot_dev=}, {self.current_rootfs_dev=}"
        )
        logger.info(
            f"{self.standby_slot=}, {self.standby_boot_dev=}, {self.standby_rootfs_dev=}"
        )

    ###### CBootControl API ######
    def get_current_slot(self) -> str:
        return self.current_slot

    def get_standby_slot(self) -> str:
        return self.standby_slot

    def get_current_rootfs_dev(self) -> str:
        return self.current_rootfs_dev

    def get_standby_rootfs_dev(self) -> str:
        return self.standby_rootfs_dev

    def get_standby_slot_partuuid(self) -> str:
        dev = self.standby_rootfs_dev
        return HelperFuncs.get_partuuid_by_dev(dev)

    def get_standby_slot(self) -> str:
        return self.standby_slot

    def get_standby_boot_dev(self) -> str:
        return self.standby_boot_dev

    def is_external_rootfs_enabled(self) -> bool:
        return self.is_rootfs_on_external

    def mark_current_slot_boot_successful(self):
        slot = self.current_slot
        Nvbootctrl.mark_boot_successful(slot)

    def set_standby_slot_unbootable(self):
        slot = self.standby_slot
        Nvbootctrl.set_slot_as_unbootable(slot)

    def switch_boot_standby(self):
        slot = self.standby_slot
        Nvbootctrl.set_active_boot_slot(slot)

    def is_current_slot_bootable(self) -> bool:
        slot = self.current_slot
        return Nvbootctrl.is_slot_bootable(slot)

    def is_current_slot_marked_successful(self) -> bool:
        slot = self.current_slot
        return Nvbootctrl.is_slot_marked_successful(slot)

    @classmethod
    def reboot(cls):
        subprocess.check_call(shlex.split("reboot"))

    def gen_extlinux_cfg(self, f: Path = None) -> str:
        """
        create extlinux config for standby slot based on imported cfg
        """
        _cboot = "${cbootargs} quiet "
        _rootfs = f"root={self.standby_slot_partuuid} rw rootwait rootfstype=ext4"
        _cmdline = f"{_cboot} {_rootfs} {self._cmdline_extra}"

        cfg = ExtlinuxCfgFile()
        if f:
            cfg.load_extlinux_cfg_file(f)
            # get the default entry from ExtLInuxCfg
            default_entry = cfg.get_default_entry()

            cmd_append = " ".join([default_entry.get("APPEND", default=""), _cmdline])
            logger.debug(f"cmdline: {cmd_append}")

            # edit default entry
            cfg.edit_entry(default_entry, "APPEND", cmd_append)
        else:
            cfg.load_entry(
                label="primary",
                menu_lable="primary kernel",
                linux=self._linux,
                initrd=self._initrd,
                fdt=self._fdt,
                append=_cmdline,
            )
        return cfg.dump_cfg()

    def write_extlinux_cfg(self, target: Path, src: Path = None):
        """
        should only be used to generate extlinux conf for standby slot
        DO NOT TOUCH THE CURRENT SLOT'S EXTLINUX FILE!

        write new extlinux conf file to target
        """
        if src and src.is_file():
            # load template extlinux_cfg from external
            cfg_text = self.gen_extlinux_cfg(src)
        else:
            # generate extlinux from default settings
            cfg_text = self.gen_extlinux_cfg()

        with open(target, "w") as f:
            f.write(cfg_text)


class CBootControlMixin(BootControlInterface):
    def _attributes_dependencies(self):
        """
        placeholder method
        attributes that needed for this mixin to work

        these attributes will be initialized in OtaClient.
        """
        self._mount_point: Path = None
        self._boot_control: CBootControl = None

        # used only when rootfs on external storage is enabled
        self._standby_boot_mount_point: Path = None

        # current slot
        self._ota_status_dir: Path = None
        self._ota_status_file: Path = None
        self._ota_version_file: Path = None
        self._slot_in_use_file: Path = None

        # standby slot
        self._standby_ota_status_dir: Path = None
        self._standby_ota_status_file: Path = None
        self._standby_ota_version_file: Path = None
        self._standby_slot_in_use_file: Path = None

    def _mount_standby(self):
        self._mount_point.mkdir(parents=True, exist_ok=True)

        standby_rootfs_dev = self._boot_control.get_standby_rootfs_dev()
        cmd_mount = f"mount {standby_rootfs_dev} {self._mount_point}"
        logger.debug(
            f"mount rootfs partition: target={standby_rootfs_dev},mount_point={self._mount_point}"
        )
        _subprocess_call(cmd_mount, raise_exception=True)
        # create new ota_status_dir on standby dev
        self._standby_ota_status_dir.mkdir(parents=True, exist_ok=True)

        # mount boot partition if rootfs on external is enabled
        # NOTE: in this case, emmc slot is used as boot partition
        if self._boot_control.is_external_rootfs_enabled():
            standby_boot_dev = self._boot_control.get_standby_boot_dev()

            self._standby_boot_mount_point.mkdir(parents=True, exist_ok=True)
            cmd_mount = f"mount {standby_boot_dev} {self._standby_boot_mount_point}"
            logger.debug(
                f"mount boot partition: target:={standby_boot_dev},mount_point={self._standby_boot_mount_point}"
            )
            _subprocess_call(cmd_mount, raise_exception=True)

    def _cleanup_standby_rootfs_parititon(self):
        """
        WARNING: apply mkfs.ext4 to standby bank
        """
        standby_dev = self._boot_control.get_standby_rootfs_dev()
        logger.warning(f"[_cleanup_standby] cleanup standby slot dev {standby_dev}")

        # first try umount the dev
        try:
            _subprocess_call(f"umount -f {standby_dev}", raise_exception=True)
        except subprocess.CalledProcessError as e:
            # suppress target not mounted error
            if e.returncode != 32:
                logger.error(f"failed to umount standby bank {standby_dev}")
                raise

        # format the standby slot
        try:
            _subprocess_call(f"mkfs.ext4 -F {standby_dev}", raise_exception=True)
        except subprocess.CalledProcessError:
            logger.error(f"failed to cleanup standby bank {standby_dev}")
            raise

    def _is_switching_boot(self) -> bool:
        # evidence 1: nvbootctrl status
        # the newly updated slot should not be marked as successful on the first reboot
        _nvboot_res = not self._boot_control.is_current_slot_marked_successful()

        # evidence 2: ota_status
        # the newly updated/rollbacked slot should have ota-status as updating/rollback
        _ota_status_res = self.load_ota_status() in [
            OtaStatus.UPDATING.name,
            OtaStatus.ROLLBACKING.name,
        ]

        # evidence 3: slot in use
        # the slot_in_use file should have the same slot as current slot
        _is_slot_in_use = (
            self.load_slot_in_use_file() == self._boot_control.get_current_slot()
        )

        logger.debug(
            f"[checking result] nvboot: {_nvboot_res}, ota_status: {_ota_status_res}, slot_in_use: {_is_slot_in_use}"
        )
        return _nvboot_res and _ota_status_res and _is_slot_in_use

    def _populate_extlinux_cfg_to_separate_bootdev(self):
        if not self._boot_control.is_external_rootfs_enabled():
            return

        src: Path = self._mount_point / Path(cfg.EXTLINUX_FILE).relative_to("/")
        target: Path = self._standby_boot_mount_point / Path(
            cfg.EXTLINUX_FILE
        ).relative_to("/")

        if src.is_file():
            shutil.copy(src, target)
        else:
            raise OtaErrorUnrecoverable(
                f"extlinux.cfg on boot partition and/or standby slot not found"
            )

    def _populate_boot_folder_to_separate_bootdev(self):
        if not self._boot_control.is_external_rootfs_enabled():
            return

        # copy the boot folder to bootdev
        # use tmp_dir folder to temporary hold the new files
        with tempfile.TemporaryDirectory(prefix=f"{__name__}_tmp_boot") as tmp_dir:
            tmp_dir: Path = Path(tmp_dir)
            dst_dir: Path = self._standby_boot_mount_point / "boot"
            shutil.copytree(
                (self._mount_point / "boot"), tmp_dir, symlinks=True, dirs_exist_ok=True
            )

            # step 1: update kernel file
            kernel_src, kernel_sig_src = (
                tmp_dir / Path(cfg.KERNEL).name,
                tmp_dir / Path(cfg.KERNEL_SIG).name,
            )
            kernel_dst, kernel_sig_dst = (
                dst_dir / Path(cfg.KERNEL).name,
                dst_dir / Path(cfg.KERNEL_SIG).name,
            )
            shutil.move(kernel_src, kernel_dst)
            shutil.move(kernel_sig_src, kernel_sig_dst)

            # step 2: update(replace) initrd
            initrd_src, initrd_dst = (
                tmp_dir / Path(cfg.INITRD).name,
                dst_dir / Path(cfg.INITRD).name,
            )
            shutil.move(initrd_src, initrd_dst)

            # NOTE: although we use initrd, not initrd.img in extlinux.cfg, we still update the initrd.img
            initrd_img_link = Path(cfg.INITRD_IMG_LINK).name
            new_initrd_link, old_initrd_link = (
                tmp_dir / initrd_img_link,
                dst_dir / initrd_img_link,
            )
            new_initrd = new_initrd_link.resolve(strict=True)
            old_initrd = old_initrd_link.resolve(strict=True)

            shutil.move(new_initrd_link, old_initrd_link)
            if old_initrd.name != new_initrd.name:
                # remove old initrd file
                # NOTE: only check the name of the initrd file
                shutil.move(new_initrd, dst_dir)
                old_initrd.unlink(missing_ok=True)

            # step 3: update(replace) dtb
            dtb_file, dtb_hdr40_file = Path(cfg.FDT).name, Path(cfg.FDT_HDR40).name
            shutil.move((tmp_dir / dtb_file), (dst_dir / dtb_file))
            shutil.move((tmp_dir / dtb_hdr40_file), (dst_dir / dtb_hdr40_file))

    def init_slot_in_use_file(self):
        """
        Note: only init current slot if needed
        """
        slot = self._boot_control.get_current_slot()
        _write_file(self._slot_in_use_file, slot)

    def load_slot_in_use_file(self):
        """
        Note: only load current slot if needed
        """
        return _read_file(self._slot_in_use_file)

    def store_slot_in_use_file(self, slot_in_use: str, target: Path):
        _write_file(target, slot_in_use)

    def load_ota_status(self) -> str:
        return _read_file(self._ota_status_file)

    def initialize_ota_status(self) -> OtaStatus:
        self._ota_status_dir.mkdir(parents=True, exist_ok=True)

        status = self.load_ota_status()
        slot_in_use = self.load_slot_in_use_file()

        logger.debug(f"active slot should be {slot_in_use}")
        logger.debug(
            f"current slot: {self._boot_control.get_current_slot()}, standby slot: {self._boot_control.get_standby_slot()}"
        )

        if len(status) == 0 or len(slot_in_use) == 0:
            self.store_initialized_ota_status()
            self.init_slot_in_use_file()
            return OtaStatus.INITIALIZED
        elif status == OtaStatus.UPDATING.name:
            return self.finalize_update()
        elif status == OtaStatus.ROLLBACKING.name:
            return self.finalize_rollback()
        elif status == OtaStatus.SUCCESS.name or status == OtaStatus.INITIALIZED.name:
            current_slot = Nvbootctrl.get_current_slot()
            if current_slot != slot_in_use:
                logger.debug(
                    f"boot into old slot {current_slot}, should boot into {slot_in_use}"
                )
                return OtaStatus.FAILURE
            else:
                return OtaStatus.SUCCESS
        else:
            return OtaStatus[status]

    def store_standby_ota_status(self, status: OtaStatus):
        _write_file(self._standby_ota_status_file, status.name)

    def store_standby_ota_version(self, version: str):
        _write_file(self._standby_ota_version_file, version)

    def store_ota_status(self, status: OtaStatus):
        _write_file(self._ota_status_file, status.name)

    def store_initialized_ota_status(self):
        self.store_ota_status(OtaStatus.INITIALIZED)
        return OtaStatus.INITIALIZED

    def get_standby_boot_partition_path(self) -> Path:
        """
        return the location of /boot folder of the mounted standby slot
        """
        return self._mount_point / "boot"

    def get_version(self) -> str:
        return _read_file(self._ota_version_file)

    def boot_ctrl_pre_update(self, version):
        logger.debug("entering pre-update...")
        # setup updating
        self._boot_control.set_standby_slot_unbootable()
        self._cleanup_standby_rootfs_parititon()
        self._mount_standby()

        # store status
        self.store_standby_ota_version(version)
        self.store_slot_in_use_file(
            self._boot_control.get_standby_slot(), self._slot_in_use_file
        )
        self.store_slot_in_use_file(
            self._boot_control.get_standby_slot(), self._standby_slot_in_use_file
        )

        logger.debug("pre-update setting finished")

    def boot_ctrl_post_update(self):
        # TODO: deal with unexpected reboot during post_update
        logger.debug("entering post-update...")
        standby_extlinux_cfg = self._mount_point / Path(cfg.EXTLINUX_FILE).relative_to(
            "/"
        )

        self._boot_control.write_extlinux_cfg(
            target=standby_extlinux_cfg, src=standby_extlinux_cfg
        )

        if self._boot_control.is_external_rootfs_enabled():
            logger.debug(
                "rootfs on external storage: updating the /boot folder in standby bootdev..."
            )
            self._populate_extlinux_cfg_to_separate_bootdev()
            self._populate_boot_folder_to_separate_bootdev()

        logger.debug("switching boot...")
        self._boot_control.switch_boot_standby()

        logger.info("post update finished, rebooting...")
        self._boot_control.reboot()

    def finalize_update(self) -> OtaStatus:
        logger.debug("entering finalizing stage...")
        if self._is_switching_boot():
            logger.debug("changes applied succeeded")
            # set the current slot(switched slot) as boot successful
            self._boot_control.mark_current_slot_boot_successful()
            self.store_ota_status(OtaStatus.SUCCESS)
            return OtaStatus.SUCCESS
        else:
            logger.warning("changes applied failed")
            self.store_ota_status(OtaStatus.FAILURE)
            return OtaStatus.FAILURE

    def boot_ctrl_post_rollback(self):
        self._boot_control.switch_boot_standby()
        self._boot_control.reboot()

    finalize_rollback = finalize_update
