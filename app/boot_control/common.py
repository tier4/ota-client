r"""Shared utils for boot_controller."""
import re
from pathlib import Path

from app.common import subprocess_check_output


class CMDHelperFuncs:
    """HelperFuncs bundle for wrapped linux cmd."""

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
