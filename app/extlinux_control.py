import io
from logging import exception
import re
from os import mkdir
from pathlib import Path
import shlex
import subprocess
from typing import Tuple
from app.ota_error import OtaErrorUnrecoverable
from app.ota_status import OtaStatus
from boot_control import BootControlMixinInterface
from configs import Config as cfg

class helpFuncWrapper:

    @classmethod
    def _findfs(cls, key: str, value: str) -> str:
        """
        findfs finds a partition by conditions
        Usage:
            findfs [options] {LABEL,UUID,PARTUUID,PARTLABEL}=<value>
        """
        _args = shlex.split(f"findfs {key}={value}")
        try:
            return subprocess.check_output(_args).decode().strip()
        except subprocess.CalledProcessError:
            return ""


    @classmethod
    def _blkid(cls, args: str) -> str:
        _args = shlex.split(f"blkid {args}")
        try:
            return subprocess.check_output(_args).decode().strip()
        except subprocess.CalledProcessError:
            return ""

    @classmethod
    def get_partuuid_by_dev(cls, dev: str) -> str:
        args = f"{dev} -s PARTUUID"
        res = cls._blkid(args)
        return res.split(':')[-1].strip(' "')

    @classmethod
    def get_dev_by_partlabel(cls, partlabel: str) -> str:
        return cls._findfs("PARTLABEL", partlabel)

class nvbootctrlWrapper:
    """
    slot num: 0->A, 1->B
    slot suffix: "", "_b"
    rootfs default label prefix: APP
    """
    _prefix = "APP"
    _active_standby_flip = { "0": "1", "1": "0"}

    @classmethod
    def _nvbootctrl(cls, arg: str) -> str: 
        # NOTE: target is always set to rootfs
        _cmd = shlex.split(f"nvbootctrl -t rootfs {arg}")
        try:
            res = subprocess.check_output(_cmd).decode().strip()
            return res
        # TODO: logger here
        except subprocess.CalledProcessError as e:
            raise e

    # nvbootctrl wrapper
    @classmethod
    def get_current_slot(cls) -> str:
        return cls._nvbootctrl("get-current-slot")
    
    @classmethod
    def get_stanby_slot(cls) -> str:
        return cls._active_standby_flip(cls.get_current_slot)

    @classmethod
    def get_suffix(cls, slot: str) -> str:
        return cls._nvbootctrl(f"get-suffix {slot}")

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
        pa = re.compile(r'\broot=(?P<rdev>[\w/]*)\b')
        rootdev = pa.match(subprocess.check_output(shlex.split("cat /proc/cmdline"))).group("rdev")
        return Path(rootdev).resolve(strict=True) == Path(dev).resolve(strict=True)

    @classmethod
    def get_current_slot_dev(cls) -> str:
        slot = cls.get_current_slot()
        suffix = cls.get_suffix(slot)
        dev = helpFuncWrapper.get_dev_by_partlabel(f"APP{suffix}")

        if not cls._check_is_rootdev(dev):
            raise OtaErrorUnrecoverable(f"rootfs mismatch, expect {dev} as rootfs")
        return dev

    @classmethod
    def get_standby_slot_dev(cls) -> str:
        slot = cls.get_stanby_slot()
        suffix = cls.get_suffix(slot)
        dev = helpFuncWrapper.get_dev_by_partlabel(f"APP{suffix}")

        if cls._check_is_rootdev(dev):
            raise OtaErrorUnrecoverable(f"rootfs mismatch, expect {dev} as standby, but it is rootdev")
        return dev

    @classmethod
    def get_standby_slot_partuuid(cls) -> str:
        dev = cls.get_standby_slot_dev()
        return helpFuncWrapper.get_partuuid_by_dev(dev)

    @classmethod
    def get_current_slot_partuuid(cls) -> str:
        dev = cls.get_current_slot_dev()
        return helpFuncWrapper.get_partuuid_by_dev(dev)

class ExtlinuxCfgFile:
    _heading = {
        "TIMEOUT": 30,
        "DEFAULT": "primary",
        "MENU TITLE": "L4T boot options"
    }

    _entry = dict()

    def set_default_entry(self, label: str):
        self._heading["DEFAULT"] = label

    def load_entry(
        self,
        label: str,
        menu_lable: str,
        fdt: str,
        linux: str="/boot/Image",
        initrd: str="/boot/initrd",
        append: str="",
    ):
        entry = dict()
        entry["MENU LABEL"] = menu_lable
        entry["LINUX"] = linux
        entry["INITRD"] = initrd
        entry["FDT"] = fdt
        entry["APPEND"] = append
        self._entry[label] = entry

    def dump_cfg(self) -> str:
        """
        dump_cfg dumps the extlinux config file to a StringIO object
        """
        buff = io.StringIO()
        _write = lambda s="": buff.write(s + '\n')
        _comment = lambda s="": buff.write('# ' + s + '\n')

        _comment("Auto generated by ota-client, DO NOT EDIT!")
        # generate heading
        for k, v in self._heading.items():
            _write(f"{k} {v}")

        # populate entry
        for t, e in self._entry.items():
            _write(f"LABEL {t}")
            for mt, mop in e.items():
                _write(f"\t{mt} {mop}")

        return buff.getvalue()

class CBootControl:
    """
    NOTE: only for tegraid:0x19, jetson xavier platform
    """
    _linux = "/boot/Image"
    _initrd = "/boot/initrd"
    _fdt = "/boot/tegra194-rqx-580.dtb"

    def __init__(self):
        (self.standby_slot, 
        self.standby_dev, 
        self.standby_partuuid) = self._get_slot_info()
        
        # check the slot info is correct
        # 1. check if the standby dev is not the same as the current rootfs dev

    def _get_slot_info(self) -> Tuple[str, str, str]:
        """
        return
            slot, dev, partuuid
        """
        _slot = nvbootctrlWrapper.get_stanby_slot()
        _dev = nvbootctrlWrapper.get_standby_slot_dev()
        _partuuid = nvbootctrlWrapper.get_standby_slot_partuuid()

        return _slot, _dev, _partuuid

    def get_standby_slot(self) -> str:
        return self.standby_slot

    def get_standby_dev(self) -> Path:
        return Path(self.standby_dev)

    def get_standby_partuuid(self) -> str:
        return self.standby_partuuid

    ###### extlinux control ######
    def _gen_cmdline(self) -> str:
        _cboot = '${cbootargs} quiet '
        _rootfs = f"root={self._standby_partuuid} rw rootwait rootfstype=ext4"
        _tty = "console=ttyTCU0,115200n8 console=tty0"
        _extra = "fbcon=map:0 net.ifnames=0"
        return f"{_cboot} {_rootfs} {_tty} {_extra}"

    def load_linux(self, path: Path):
        """
        overide the default linux image path
        """
        if path.is_file():
            self._linux = str(path)
    
    def load_initrd(self, path: Path):
        if path.is_file():
            self._initrd = str(path)

    def load_fdt(self, path: Path):
        if path.is_file():
            self._fdt = str(path)

    def gen_extlinux_cfg(self) -> str:
        """
        create extlinux config for standby slot
        """
        cfg = ExtlinuxCfgFile()
        cfg.load_entry(
            label="primary",
            menu_lable="primary kernel",
            linux=self._linux,
            initrd=self._initrd,
            fdt=self._fdt,
            append=self._gen_cmdline(),
        )
        return cfg.dump_cfg()

    def write_extlinux_cfg(self, target: Path):
        """
        write new extlinux conf file to target
        """
        with open(target, "w") as f:
            f.write(self.gen_extlinux_cfg)

    ###### nvbootctrl ######
    @classmethod
    def mark_current_slot_boot_successful(cls):
        slot = nvbootctrlWrapper.get_current_slot()
        nvbootctrlWrapper.mark_boot_successful(slot)

    @classmethod
    def set_standby_slot_unbootable(cls):
        slot = nvbootctrlWrapper.get_stanby_slot()
        nvbootctrlWrapper.set_slot_as_unbootable(slot)

    @classmethod
    def switch_boot_standby(cls):
        dev = nvbootctrlWrapper.get_stanby_slot()
        nvbootctrlWrapper.set_active_boot_slot(dev)
    
    @classmethod
    def is_current_slot_bootable(cls) -> bool:
        slot = nvbootctrlWrapper.get_current_slot()
        return nvbootctrlWrapper.is_slot_bootable(slot)

    @classmethod
    def is_current_slot_marked_successful(cls) -> bool:
        slot = nvbootctrlWrapper.get_current_slot()
        return nvbootctrlWrapper.is_slot_marked_successful(slot)

    @classmethod
    def reboot(cls):
        subprocess.check_call(shlex.split("reboot"))

def _read_file(path: Path) -> str:
    try:
        return path.read_text().strip()
    except:
        return ""

def _write_file(path: Path, input: str):
    path.write_text(input)

class CBootControlMixin(BootControlMixinInterface):

    def __init__(self):
        self._boot_control = CBootControl()
        self._mount_point: Path = cfg.MOUNT_POINT
        
        # TODO: hardcoded status file name
        # current slot
        self._ota_status_dir: Path = cfg.OTA_STATUS_DIR
        self._ota_status_dir.mkdir(parents=True, exist_ok=True)
        self._ota_status_file = self._ota_status_dir / "status"
        self._ota_version_file = self._ota_status_dir / "version"

        # standby slot
        self._standby_ota_status_dir: Path = self._mount_point / cfg.OTA_STATUS_DIR.relative_to(Path("/"))
        self._standby_ota_status_file = self._standby_ota_status_dir / "status"
        self._standby_ota_version_file = self._standby_ota_status_dir / "version"

    def _mount_standby(self):
        standby_dev = self._boot_control.get_standby_dev()
        cmd_mount = f"mount {standby_dev} {self._mount_point}"
        subprocess.check_call(shlex.split(cmd_mount))

    def _cleanup_standby(self):
        """
        WARNING: apply mkfs.ext4 to standby bank
        """
        standby_dev = self._boot_control.get_standby_dev()
        _cmd = shlex.split(f"mkfs.ext4 {standby_dev}")
        try:
            subprocess.check_call(_cmd)
        except subprocess.CalledProcessError as e:
            raise e

    def _is_switching_boot(self) -> bool:

        # evidence 1: nvbootctrl status
        # the newly updated slot should not be marked as successful on the first reboot
        _nvboot_res = \
            not self._boot_control.is_current_slot_marked_successful()
        
        # evidence 2: ota_status
        # the newly updated slot should have ota-status as updating or rollback
        _ota_status_res = self.load_ota_status() in \
            [OtaStatus.UPDATING.name, OtaStatus.ROLLBACKING.name]

        return not _nvboot_res and _ota_status_res

    def load_ota_status(self) -> str:
        return _read_file(self._ota_status_file)

    def initialize_ota_status(self) -> OtaStatus:
        status = self.load_ota_status()
        if len(status) == 0:
            self.write_initialized_ota_status()
            return OtaStatus.INITIALIZED
        elif status == OtaStatus.UPDATING.name:
            return self.finalize_update()
        elif status == OtaStatus.ROLLBACKING.name:
            return self.finalize_rollback()
        else:
            return OtaStatus[status]

    def write_standby_ota_status(self, status: OtaStatus):
        _write_file(self._standby_ota_status_file, status.name)

    def write_standby_ota_version(self, version: str):
        _write_file(self._standby_ota_version_file, version)
        
    def write_current_ota_status(self, status: OtaStatus):
        _write_file(self._ota_status_file, status.name)

    def write_initialized_ota_status(self):
        self.write_standby_ota_status(OtaStatus.INITIALIZED)
        return OtaStatus.INITIALIZED

    def get_standby_boot_partition_path(self) -> Path:
        return self._boot_control.get_standby_dev()

    def get_version(self):
        return _read_file(self._ota_version_file)

    def boot_ctrl_pre_update(self, version):
        self.write_standby_ota_status(OtaStatus.UPDATING)
        self.write_standby_ota_version(version)

        self._boot_control.set_standby_slot_unbootable()
        self._cleanup_standby()
        self._mount_standby()

    def boot_ctrl_post_update(self):
        extlinux_cfg: Path = self._mount_point / cfg.EXLINUX_FILE.relative_to('/')
        self._boot_control.write_extlinux_cfg(extlinux_cfg)
        self._boot_control.switch_boot_standby()
        self._boot_control.reboot()
        
    def finalize_update(self) -> OtaStatus:
        if not self._is_switching_boot():
            self.write_current_ota_status(OtaStatus.FAILURE)
            # TODO: should I switch back to original slot?
            # set active slot back to the previous slot
            self._boot_control.switch_boot_standby()
            return OtaStatus.FAILURE
        else:
            # set the current slot(switched slot) as boot successful
            self._boot_control.mark_current_slot_boot_successful()
            self.write_current_ota_status(OtaStatus.SUCCESS)
            return OtaStatus.SUCCESS