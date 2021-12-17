import io
import re
import shlex
import subprocess
from pathlib import Path
from typing import Tuple

import log_util
from configs import config as cfg
from ota_error import OtaErrorUnrecoverable
from ota_status import OtaStatus
from ota_client_interface import BootControlInterface

assert cfg.PLATFORM == "cboot"

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


def _read_file(path: Path) -> str:
    try:
        return path.read_text().strip()
    except Exception:
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
    def _blkid(args: str) -> str:
        _cmd = f"blkid {args}"
        return _subprocess_check_output(_cmd)

    @staticmethod
    def _findmnt(args: str) -> str:
        _cmd = f"findmnt {args}"
        return _subprocess_check_output(_cmd)

    @staticmethod
    def _lsblk(args: str) -> str:
        _cmd = f"lsblk {args}"
        return _subprocess_check_output(_cmd)

    @classmethod
    def get_partuuid_by_dev(cls, dev: str) -> str:
        args = f"{dev} -s PARTUUID"
        res = cls._blkid(args)

        pa = re.compile(r'PARTUUID="?(?P<partuuid>[\w-]*)"?')
        v = pa.search(res).group("partuuid")

        return f"PARTUUID={v}"

    @classmethod
    def get_dev_by_partuuid(cls, partuuid: str) -> str:
        return cls._findfs("PARTUUID", partuuid)

    @classmethod
    def get_rootfs_dev(cls) -> str:
        return cls._findmnt("/ -o SOURCE -n")

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
        return cls._lsblk(cmd).split("\n")

    @classmethod
    def get_sibling_dev(cls, device: str) -> str:
        """
        When `/dev/nvme0n1p1` is specified as child_device, /dev/nvme0n1p2 is returned.
        """
        parent = cls.get_parent_dev(device)
        family = cls.get_family_devs(parent)
        pa = re.compile(r'NAME="(?P<dev>.*)"')
        for blk in family:
            m = pa.match(blk)
            dev = m.group("dev")
            if dev != parent and dev != device:
                return dev
        raise OtaErrorUnrecoverable(f"lsblk output={family} is illegal")


class Nvbootctrl:
    """
    slot num: 0->A, 1->B
    """

    CURRENT_STANDBY_FLIP = {"0": "1", "1": "0"}

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

    # nvbootctrl wrapper
    @classmethod
    def get_current_slot(cls) -> str:
        return cls._nvbootctrl("get-current-slot", call_only=False)

    @classmethod
    def get_standby_slot(cls) -> str:
        return cls.CURRENT_STANDBY_FLIP[cls.get_current_slot()]

    @classmethod
    def get_suffix(cls, slot: str) -> str:
        return cls._nvbootctrl(f"get-suffix {slot}", call_only=False)

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

    # helper functions derived from nvbootctrl wrapper funcs
    @classmethod
    def _check_is_rootdev(cls, dev: str) -> bool:
        """
        check the givin dev is root dev or not
        """
        pa = re.compile(r"\broot=(?P<rdev>[\w=-]*)\b")
        ma = pa.search(_subprocess_check_output("cat /proc/cmdline")).group("rdev")
        uuid = ma.split("=")[-1]

        return Path(HelperFuncs.get_dev_by_partuuid(uuid)).resolve(strict=True) == Path(
            dev
        ).resolve(strict=True)

    @classmethod
    def get_current_slot_dev(cls) -> str:
        dev = HelperFuncs.get_rootfs_dev()

        if not cls._check_is_rootdev(dev):
            raise OtaErrorUnrecoverable(f"rootfs mismatch, expect {dev} as rootfs")

        logger.debug(f"current slot dev: {dev}")
        return dev

    @classmethod
    def get_standby_slot_dev(cls) -> str:
        rootfs_dev = HelperFuncs.get_rootfs_dev()
        dev = HelperFuncs.get_sibling_dev(rootfs_dev)

        if cls._check_is_rootdev(dev):
            msg = (
                f"rootfs mismatch, expect {dev} as standby slot dev, but it is rootdev"
            )
            logger.error(msg)
            raise OtaErrorUnrecoverable(msg)

        logger.debug(f"standby slot dev: {dev}")
        return dev

    @classmethod
    def get_standby_slot_partuuid(cls) -> str:
        dev = cls.get_standby_slot_dev()
        return HelperFuncs.get_partuuid_by_dev(dev)

    @classmethod
    def get_current_slot_partuuid(cls) -> str:
        dev = cls.get_current_slot_dev()
        return HelperFuncs.get_partuuid_by_dev(dev)


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
        self._linux = cfg.LINUX
        self._initrd = cfg.INITRD
        self._fdt = cfg.FDT
        self._cmdline_extra = cfg.EXTRA_CMDLINE

        (
            self._standby_slot,
            self._standby_dev,
            self._standby_partuuid,
        ) = self._get_slot_info()

        # NOTE: only support r580 platform right now!
        # detect the chip id
        tegra_chip_id_f = Path("/sys/module/tegra_fuse/parameters/tegra_chip_id")
        self.chip_id = int(_read_file(tegra_chip_id_f).strip())
        if self.chip_id not in cfg.CHIP_ID_MODEL_MAP:
            raise NotImplementedError(
                f"unsupported platform found (chip_id: {self.chip_id}), abort"
            )

        self.model = cfg.CHIP_ID_MODEL_MAP[self.chip_id]

        logger.debug(f"standby slot: {self._standby_slot}")
        logger.debug(f"standby dev: {self._standby_dev}")
        logger.debug(f"standby dev partuuid: {self._standby_partuuid}")
        logger.info(f"model: {self.model} (chip_id: {hex(self.chip_id)})")

    def _get_slot_info(self) -> Tuple[str, str, str]:
        """
        return
            slot, dev, partuuid
        """
        _slot = Nvbootctrl.get_standby_slot()
        _dev = Nvbootctrl.get_standby_slot_dev()
        _partuuid = Nvbootctrl.get_standby_slot_partuuid()

        return _slot, _dev, _partuuid

    def get_standby_slot(self) -> str:
        return self._standby_slot

    def get_standby_dev(self) -> Path:
        return Path(self._standby_dev)

    def get_standby_partuuid(self) -> str:
        return self._standby_partuuid

    ###### extlinux control ######
    def _gen_cmdline(self) -> str:
        _cboot = "${cbootargs} quiet "
        _rootfs = f"root={self._standby_partuuid} rw rootwait rootfstype=ext4"
        return f"{_cboot} {_rootfs} {self._cmdline_extra}"

    def gen_extlinux_cfg(self, f: Path = None) -> str:
        """
        create extlinux config for standby slot based on imported cfg
        """
        cfg = ExtlinuxCfgFile()
        if f:
            cfg.load_extlinux_cfg_file(f)
            # get the default entry from ExtLInuxCfg
            default_entry = cfg.get_default_entry()
            cmd_append = " ".join(
                [default_entry.get("APPEND", default=""), self._gen_cmdline()]
            )
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
                append=self._gen_cmdline(),
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

    ###### nvbootctrl ######
    @classmethod
    def mark_current_slot_boot_successful(cls):
        slot = Nvbootctrl.get_current_slot()
        Nvbootctrl.mark_boot_successful(slot)

    @classmethod
    def set_standby_slot_unbootable(cls):
        slot = Nvbootctrl.get_standby_slot()
        Nvbootctrl.set_slot_as_unbootable(slot)

    @classmethod
    def switch_boot_standby(cls):
        dev = Nvbootctrl.get_standby_slot()
        Nvbootctrl.set_active_boot_slot(dev)

    @classmethod
    def is_current_slot_bootable(cls) -> bool:
        slot = Nvbootctrl.get_current_slot()
        return Nvbootctrl.is_slot_bootable(slot)

    @classmethod
    def is_current_slot_marked_successful(cls) -> bool:
        slot = Nvbootctrl.get_current_slot()
        return Nvbootctrl.is_slot_marked_successful(slot)

    @classmethod
    def reboot(cls):
        subprocess.check_call(shlex.split("reboot"))


class CBootControlMixin(BootControlInterface):
    def _attributes_dependencies(self):
        """
        placeholder method
        attributes that needed for this mixin to work

        these attributes will be initialized in OtaClient.
        """
        self._mount_point: Path = None
        self._boot_control: CBootControl = None

        # current slot
        self._ota_status_dir: Path = None
        self._ota_status_file: Path = None
        self._ota_version_file: Path = None
        self._slot_in_use_file: Path = None

        # standby slot
        self._standby_ota_status_dir: Path = None
        self._standby_ota_status_file: Path = None
        self._standby_ota_version_file: Path = None
        self._standby_extlinux_cfg: Path = None
        self._standby_slot_in_use_file: Path = None

    def _mount_standby(self):
        self._mount_point.mkdir(parents=True, exist_ok=True)

        standby_dev = self._boot_control.get_standby_dev()
        cmd_mount = f"mount {standby_dev} {self._mount_point}"
        logger.debug(f"target: {standby_dev}, mount_point: {self._mount_point}")

        _subprocess_call(cmd_mount, raise_exception=True)
        # create new ota_status_dir on standby dev
        self._standby_ota_status_dir.mkdir(parents=True, exist_ok=True)

    def _cleanup_standby(self):
        """
        WARNING: apply mkfs.ext4 to standby bank
        """
        standby_dev = self._boot_control.get_standby_dev()
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
        _is_slot_in_use = self.load_slot_in_use_file() == Nvbootctrl.get_current_slot()

        logger.debug(
            f"[checking result] nvboot: {_nvboot_res}, ota_status: {_ota_status_res}, slot_in_use: {_is_slot_in_use}"
        )
        return _nvboot_res and _ota_status_res and _is_slot_in_use

    def init_slot_in_use_file(self):
        """
        Note: only init current slot if needed
        """
        slot = Nvbootctrl.get_current_slot()
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
            f"current slot: {Nvbootctrl.get_current_slot()}, standby slot: {Nvbootctrl.get_standby_slot()}"
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
        self._cleanup_standby()
        self._mount_standby()

        # store status
        self.store_standby_ota_version(version)
        self.store_slot_in_use_file(
            Nvbootctrl.get_standby_slot(), self._slot_in_use_file
        )
        self.store_slot_in_use_file(
            Nvbootctrl.get_standby_slot(), self._standby_slot_in_use_file
        )

        logger.debug("pre-update setting finished")

    def boot_ctrl_post_update(self):
        logger.debug("entering post-update...")
        self._boot_control.write_extlinux_cfg(
            target=self._standby_extlinux_cfg, src=self._standby_extlinux_cfg
        )

        self._boot_control.switch_boot_standby()
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
