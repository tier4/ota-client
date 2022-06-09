r"""Shared utils for boot_controller."""
import re
from enum import auto, Enum
from pathlib import Path
from subprocess import CalledProcessError
from typing import List

from app import log_util
from app.configs import config as cfg
from app.common import subprocess_call, subprocess_check_output


logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class MountFailedReason(Enum):
    SUCCESS = 0
    PERMISSIONS_ERROR = 1
    SYSTEM_ERROR = 2
    INTERNAL_ERROR = 4
    USER_INTERRUPT = 8
    GENERIC_MOUNT_FAILURE = 32
    # specific reason for generic mount failure
    TARGET_NOT_FOUND = auto()
    TARGET_ALREADY_MOUNTED = auto()
    MOUNT_POINT_NOT_FOUND = auto()
    BIND_MOUNT_ON_NON_DIR = auto()

    @classmethod
    def parse_failed_reason(cls, e: CalledProcessError) -> "MountFailedReason":
        _reason = cls(e.returncode)
        if _reason != cls.GENERIC_MOUNT_FAILURE:
            return _reason

        # if the return code is 32, determine the detailed reason
        # of the mount failure
        _error_msg = str(e.stderr)
        if _error_msg.find("already mounted") != -1:
            return cls.TARGET_ALREADY_MOUNTED
        elif _error_msg.find("mount point does not exist") != -1:
            return cls.MOUNT_POINT_NOT_FOUND
        elif _error_msg.find("does not exist") != -1:
            return cls.TARGET_NOT_FOUND
        elif _error_msg.find("Not a directory") != -1:
            return cls.BIND_MOUNT_ON_NON_DIR
        else:
            return cls.GENERIC_MOUNT_FAILURE


class CMDHelperFuncs:
    """HelperFuncs bundle for wrapped linux cmd."""

    @staticmethod
    def _mount(
        dev: str, mount_point: str, options: List[str] = None
    ) -> MountFailedReason:
        """
        mount [-o option1[,option2, ...]]] <dev> <mount_point>
        """
        _option_str = ""
        if options:
            _option_str = f"-o {','.join(options)}"

        _cmd = f"mount {_option_str} {dev} {mount_point}"
        try:
            subprocess_call(_cmd, raise_exception=True)
        except CalledProcessError as e:
            return MountFailedReason.parse_failed_reason(e)
        finally:
            return MountFailedReason.SUCCESS

    @staticmethod
    def _findfs(key: str, value: str) -> str:
        """
        findfs finds a partition by conditions
        Usage:
            findfs [options] {LABEL,UUID,PARTUUID,PARTLABEL}=<value>
        """
        _cmd = f"findfs {key}={value}"
        return subprocess_check_output(_cmd)

    @staticmethod
    def _findmnt(p: str) -> str:
        _cmd = f"findmnt {p}"
        return subprocess_check_output(_cmd)

    @staticmethod
    def _lsblk(args: str) -> str:
        _cmd = f"lsblk {args}"
        return subprocess_check_output(_cmd)

    @staticmethod
    def _blkid(args: str) -> str:
        _cmd = f"blkid {args}"
        return subprocess_check_output(_cmd)

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
    def get_mount_point_by_dev(cls, dev: str) -> str:
        """
        findmnt <dev> -o TARGET -n
        """
        mount_point = cls._findmnt(f"{dev} -o TARGET -n")
        if not mount_point:
            raise ValueError(f"{dev} is not mounted")

        return mount_point

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
        raise ValueError(f"device is has unexpected partition layout, {family=}")

    @classmethod
    def mount(
        cls, target: str, mount_point: str, options: List[str] = None
    ) -> MountFailedReason:
        return cls._mount(target, mount_point, options)
