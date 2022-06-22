r"""Shared utils for boot_controller."""
import re
from abc import abstractmethod
from enum import auto, Enum
from pathlib import Path
from subprocess import CalledProcessError
from typing import List, Optional, Protocol, Union

from app import log_util
from app.configs import config as cfg
from app.common import (
    read_from_file,
    subprocess_call,
    subprocess_check_output,
    write_to_file,
)
from app.ota_status import OTAStatusEnum


logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)

# fmt: off
class BootControllerProtocol(Protocol):
    @abstractmethod
    def get_ota_status(self) -> OTAStatusEnum: ...
    @abstractmethod
    def get_standby_slot_path(self) -> Path: ...
    @abstractmethod
    def get_standby_boot_dir(self) -> Path: ...
    @abstractmethod
    def pre_update(self, version: str, *, standby_as_ref: bool, erase_standby: bool): ...
    @abstractmethod
    def post_update(self): ...
    @abstractmethod
    def post_rollback(self): ...
    @abstractmethod
    def load_version(self) -> str: ...

# fmt: on


class BootControlError(Exception):
    pass


class BootControlExternalError(BootControlError):
    """Error caused by calling external program.

    For ota-client, typically we map this Error as Recoverable.
    """


class BootControlInternalError(BootControlError):
    """Error caused by boot control internal logic.

    For ota-client, typically we map this Error as Unrecoverable.
    """


class MountFailedReason(Enum):
    # error code
    SUCCESS = 0
    PERMISSIONS_ERROR = 1
    SYSTEM_ERROR = 2
    INTERNAL_ERROR = 4
    USER_INTERRUPT = 8
    GENERIC_MOUNT_FAILURE = 32

    # custom error code
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
        dev: str, mount_point: str, options: Optional[List[str]] = None
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
            return MountFailedReason.SUCCESS
        except CalledProcessError as e:
            return MountFailedReason.parse_failed_reason(e)

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

    ###### derived helper methods ######

    @classmethod
    def get_partuuid_by_dev(cls, dev: str) -> str:
        args = f"{dev} -s PARTUUID"
        res = cls._blkid(args)

        pa = re.compile(r'PARTUUID="?(?P<partuuid>[\w-]*)"?')
        if ma := pa.search(res):
            v = ma.group("partuuid")
        else:
            logger.error(f"failed to get partuuid from {dev}")
            return ""

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
    def get_mount_point_by_dev(cls, dev: str, *, raise_exception=True) -> str:
        """
        findmnt <dev> -o TARGET -n
        """
        mount_point = cls._findmnt(f"{dev} -o TARGET -n")
        if not mount_point and raise_exception:
            raise BootControlExternalError(f"{dev} is not mounted")

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
        family: List[str] = cls.get_family_devs(cls.get_parent_dev(device))
        partitions = {i.split("=")[-1].strip('"') for i in family[1:3]}
        res = partitions - {device}
        if len(res) == 1:
            (r,) = res
            return r

        raise BootControlExternalError(
            f"device is has unexpected partition layout, {family=}"
        )

    @classmethod
    def mount(
        cls,
        target: str,
        mount_point: Union[Path, str],
        options: Optional[List[str]] = None,
    ):
        _failed_reason = cls._mount(target, str(mount_point), options)
        if _failed_reason != MountFailedReason.SUCCESS:
            _failure_msg = (
                f"failed to mount {target} on {mount_point}: {_failed_reason.name}"
            )
            logger.error(_failure_msg)
            raise BootControlExternalError(_failure_msg)

    @classmethod
    def umount_dev(cls, dev: Union[Path, str]):
        try:
            _cmd = f"umount -f {dev}"
            subprocess_call(_cmd, raise_exception=True)
        except CalledProcessError as e:
            # ignore if target dev is not mounted
            if e.returncode == 32 and str(e.stderr).find("not mounted") != -1:
                return

            _failure_msg = f"failed to umount {dev}: {e!r}"
            logger.warning(_failure_msg)
            raise BootControlExternalError(_failure_msg)

    @classmethod
    def mkfs_ext4(cls, dev: str):
        try:
            _cmd = f"mkfs.ext4 -F {dev}"
            subprocess_call(_cmd, raise_exception=True)
        except CalledProcessError as e:
            _failure_msg = f"failed to apply mkfs.ext4 on {dev}: {e!r}"
            logger.error(_failure_msg)
            raise BootControlExternalError(_failure_msg)

    @classmethod
    def mount_refroot(
        cls,
        standby_slot_dev: str,
        active_slot_dev: str,
        refroot_mount_point: str,
        *,
        standby_as_ref: bool,
    ):
        _refroot_dev = standby_slot_dev if standby_as_ref else active_slot_dev

        # NOTE: set raise_exception to false to allow not mounted
        # not mounted dev will have empty return str
        if _refroot_active_mount_point := CMDHelperFuncs.get_mount_point_by_dev(
            _refroot_dev, raise_exception=False
        ):
            _mount_options = ["bind", "ro"]
            CMDHelperFuncs.mount(
                _refroot_active_mount_point,
                refroot_mount_point,
                _mount_options,
            )
        else:
            # NOTE: refroot is expected to be mounted,
            # if refroot is active rootfs, it is mounted as /;
            # if refroot is standby rootfs(in-place update mode),
            # it will be mounted to /mnt/standby(rw), so we still mount it as bind,ro
            raise BootControlInternalError("refroot is expected to be mounted")


###### helper mixins ######
class SlotInUseMixin:
    current_ota_status_dir: Path
    standby_ota_status_dir: Path

    def _store_current_slot_in_use(self, _slot: str):
        write_to_file(self.current_ota_status_dir / cfg.SLOT_IN_USE_FNAME, _slot)

    def _store_standby_slot_in_use(self, _slot: str):
        write_to_file(self.standby_ota_status_dir / cfg.SLOT_IN_USE_FNAME, _slot)

    def _load_current_slot_in_use(self) -> str:
        return read_from_file(
            self.standby_ota_status_dir / cfg.SLOT_IN_USE_FNAME, missing_ok=False
        )


class OTAStatusMixin:
    current_ota_status_dir: Path
    standby_ota_status_dir: Path
    ota_status: OTAStatusEnum

    def _store_current_ota_status(self, _status: OTAStatusEnum):
        """NOTE: only update the current ota_status at ota-client launching up!"""
        write_to_file(self.current_ota_status_dir / cfg.OTA_STATUS_FNAME, _status.name)

    def _store_standby_ota_status(self, _status: OTAStatusEnum):
        write_to_file(self.standby_ota_status_dir / cfg.OTA_STATUS_FNAME, _status.name)

    def _load_current_ota_status(self) -> OTAStatusEnum:
        _status = OTAStatusEnum.FAILURE  # for unexpected situation, default to FAILURE
        try:
            _status_str = read_from_file(
                self.current_ota_status_dir / cfg.OTA_STATUS_FNAME
            ).upper()
            _status = OTAStatusEnum[_status_str]
        except KeyError:
            _status = OTAStatusEnum.INITIALIZED
            write_to_file(
                self.current_ota_status_dir / cfg.OTA_STATUS_FNAME, _status.name
            )
        finally:
            return _status

    def get_ota_status(self) -> OTAStatusEnum:
        return self.ota_status


class VersionControlMixin:
    current_ota_status_dir: Path
    standby_ota_status_dir: Path

    def _store_standby_version(self, _version: str):
        try:
            write_to_file(self.standby_ota_status_dir / cfg.OTA_VERSION_FNAME, _version)
        except FileNotFoundError as e:
            raise BootControlExternalError from e

    def load_version(self) -> str:
        _version = read_from_file(
            self.current_ota_status_dir / cfg.OTA_VERSION_FNAME, missing_ok=True
        )
        if not _version:
            logger.warning("version file not found, return empty version string")

        return _version
