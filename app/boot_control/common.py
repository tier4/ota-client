r"""Shared utils for boot_controller."""
from enum import auto, Enum
from pathlib import Path
from subprocess import CalledProcessError
from typing import Callable, List, Optional, Union

from app import log_util
from app.configs import config as cfg
from app.common import (
    read_from_file,
    subprocess_call,
    subprocess_check_output,
    write_to_file_sync,
)
from app.ota_status import OTAStatusEnum


logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class _BootControlError(Exception):
    """Internal used exception type related to boot control errors.
    NOTE: this exception should not be directly raised to upper caller,
    it should always be wrapped by a specific OTA Error type.
    check app.errors for details.
    """


class MountError(_BootControlError):
    def __init__(
        self, *args: object, fail_reason: Optional["MountFailedReason"] = None
    ) -> None:
        super().__init__(*args)
        self.fail_reason = fail_reason


class ABPartitionError(_BootControlError):
    ...


class MkfsError(_BootControlError):
    ...


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
    def parse_failed_reason(cls, func: Callable):
        from functools import wraps

        @wraps(func)
        def _wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except CalledProcessError as e:
                _called_process_error_str = (
                    f"{e.cmd=} failed: {e.returncode=}, {e.stderr=}, {e.stdout=}"
                )

                _reason = cls(e.returncode)
                if _reason != cls.GENERIC_MOUNT_FAILURE:
                    raise MountError(
                        _called_process_error_str,
                        fail_reason=_reason,
                    ) from None

                # if the return code is 32, determine the detailed reason
                # of the mount failure
                _error_msg, _fail_reason = str(e.stderr), cls.GENERIC_MOUNT_FAILURE
                if _error_msg.find("already mounted") != -1:
                    _fail_reason = cls.TARGET_ALREADY_MOUNTED
                elif _error_msg.find("mount point does not exist") != -1:
                    _fail_reason = cls.MOUNT_POINT_NOT_FOUND
                elif _error_msg.find("does not exist") != -1:
                    _fail_reason = cls.TARGET_NOT_FOUND
                elif _error_msg.find("Not a directory") != -1:
                    _fail_reason = cls.BIND_MOUNT_ON_NON_DIR

                raise MountError(
                    _called_process_error_str,
                    fail_reason=_fail_reason,
                ) from None

        return _wrapper


class CMDHelperFuncs:
    """HelperFuncs bundle for wrapped linux cmd."""

    @staticmethod
    @MountFailedReason.parse_failed_reason
    def _mount(
        dev: str,
        mount_point: str,
        *,
        options: Optional[List[str]] = None,
        args: Optional[List[str]] = None,
    ):
        """
        mount [-o option1[,option2, ...]]] [args[0] [args[1]...]] <dev> <mount_point>
        """
        _option_str = ""
        if options:
            _option_str = f"-o {','.join(options)}"

        _args_str = ""
        if args:
            _args_str = f"{' '.join(args)}"

        _cmd = f"mount {_option_str} {_args_str} {dev} {mount_point}"
        subprocess_call(_cmd, raise_exception=True)

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
    def _findmnt(args: str) -> str:
        _cmd = f"findmnt {args}"
        return subprocess_check_output(_cmd)

    @staticmethod
    def _lsblk(args: str, *, raise_exception=False) -> str:
        _cmd = f"lsblk {args}"
        return subprocess_check_output(_cmd, raise_exception=raise_exception)

    ###### derived helper methods ######

    @classmethod
    def get_partuuid_by_dev(cls, dev: str) -> str:
        """Return partuuid of input device."""
        args = f"-in -o PARTUUID {dev}"
        try:
            return cls._lsblk(args, raise_exception=True)
        except ValueError as e:
            msg = f"failed to get partuuid for {dev}: {e}"
            raise ValueError(msg) from None

    @classmethod
    def get_uuid_by_dev(cls, dev: str) -> str:
        """Return uuid of input device."""
        args = f"-in -o UUID {dev}"
        try:
            return cls._lsblk(args, raise_exception=True)
        except ValueError as e:
            msg = f"failed to get uuid for {dev}: {e}"
            raise ValueError(msg) from None

    @classmethod
    def get_uuid_str_by_dev(cls, dev: str) -> str:
        """Return UUID string of input device.

        Returns:
            str like: "UUID=<uuid>"
        """
        return f"UUID={cls.get_uuid_by_dev(dev)}"

    @classmethod
    def get_partuuid_str_by_dev(cls, dev: str) -> str:
        """Return PARTUUID string of input device.

        Returns:
            str like: "PARTUUID=<partuuid>"
        """
        return f"PARTUUID={cls.get_partuuid_by_dev(dev)}"

    @classmethod
    def get_dev_by_partlabel(cls, partlabel: str) -> str:
        return cls._findfs("PARTLABEL", partlabel)

    @classmethod
    def get_dev_by_partuuid(cls, partuuid: str) -> str:
        return cls._findfs("PARTUUID", partuuid)

    @classmethod
    def get_current_rootfs_dev(cls) -> str:
        """
        NOTE:
            -o <COLUMN>: only print <COLUMN>
            -n: no headings
            -f: only show the first file system
            -c: canonicalize printed paths

        Returns:
            full path to dev of the current rootfs
        """
        if res := cls._findmnt("/ -o SOURCE -n -f -c"):
            return res
        else:
            raise _BootControlError("failed to detect current rootfs")

    @classmethod
    def get_mount_point_by_dev(cls, dev: str, *, raise_exception=True) -> str:
        """
        findmnt <dev> -o TARGET -n

        NOTE: findmnt raw result might have multiple lines if target dev is bind mounted.
        use option -f to only show the first file system
        """
        mount_points = cls._findmnt(f"{dev} -o TARGET -n -f")
        if not mount_points and raise_exception:
            raise MountError(f"{dev} is not mounted")

        return mount_points

    @classmethod
    def get_dev_by_mount_point(cls, mount_point: str) -> str:
        """Return the underlying mounted dev of the given mount_point."""
        return cls._findmnt(f"-no SOURCE {mount_point}")

    @classmethod
    def is_target_mounted(cls, target: Union[Path, str]) -> bool:
        return cls._findmnt(f"{target}") != ""

    @classmethod
    def get_parent_dev(cls, child_device: str) -> str:
        """
        When `/dev/nvme0n1p1` is specified as child_device, /dev/nvme0n1 is returned.

        cmd params:
            -d: print the result from specified dev only.
        """
        cmd = f"-idpn -o PKNAME {child_device}"
        if res := cls._lsblk(cmd):
            return res
        else:
            raise ValueError(f"{child_device} not found or not a partition")

    @classmethod
    def get_dev_family(cls, parent_device: str, *, include_parent=True) -> List[str]:
        """
        When `/dev/nvme0n1` is specified as parent_device,
        ["/dev/nvme0n1", "/dev/nvme0n1p1", "/dev/nvme0n1p2"...] will be return
        """
        cmd = f"-Pp -o NAME {parent_device}"
        res = list(
            map(
                lambda l: l.split("=")[-1].strip('"'),
                cls._lsblk(cmd).splitlines(),
            )
        )

        # the first line is the parent of the dev family
        if include_parent:
            return res
        else:
            return res[1:]

    @classmethod
    def get_dev_size(cls, dev: str) -> int:
        """Return the size of dev by bytes.

        NOTE:
            -d: print the result from specified dev only
            -b: print size in bytes
            -n: no headings
        """
        cmd = f"-dbn -o SIZE {dev}"
        try:
            return int(cls._lsblk(cmd))
        except ValueError:
            raise ValueError(f"failed to get size of {dev}") from None

    @classmethod
    def mount_rw(cls, target: str, mount_point: Union[Path, str]):
        """
        NOTE: pass args = ["--make-private", "--make-unbindable"] to prevent
        mount events propagation to/from this mount point.
        """
        options = ["rw"]
        args = ["--make-private", "--make-unbindable"]
        cls._mount(target, str(mount_point), options=options, args=args)

    @classmethod
    def bind_mount_ro(cls, target: str, mount_point: Union[Path, str]):
        """
        NOTE: pass args = ["--make-private", "--make-unbindable"] to prevent
        mount events propagation to/from this mount point.
        """
        options = ["bind", "ro"]
        args = ["--make-private", "--make-unbindable"]
        cls._mount(target, str(mount_point), options=options, args=args)

    @classmethod
    def umount(cls, target: Union[Path, str], *, ignore_error=False):
        # first try to check whether the target(either a mount point or a dev)
        # is mounted
        if not cls.is_target_mounted(target):
            return

        # if the target is mounted, try to unmount it.
        try:
            _cmd = f"umount -f {target}"
            subprocess_call(_cmd, raise_exception=True)
        except CalledProcessError as e:
            _failure_msg = (
                f"failed to umount {target}: {e.returncode=}, {e.stderr=}, {e.stdout=}"
            )
            logger.warning(_failure_msg)

            if not ignore_error:
                raise _BootControlError(_failure_msg) from None

    @classmethod
    def mkfs_ext4(cls, dev: str):
        try:
            logger.warning(f"format {dev} to ext4...")
            # inherit previous uuid
            _uuid = cls.get_uuid_by_dev(dev)
            _cmd = f"mkfs.ext4 -F -U {_uuid} {dev}"
            subprocess_call(_cmd, raise_exception=True)
        except CalledProcessError as e:
            _failure_msg = f"failed to apply mkfs.ext4 on {dev}: {e!r}"
            logger.error(_failure_msg)
            raise MkfsError(_failure_msg)

    @classmethod
    def mount_refroot(
        cls,
        standby_slot_dev: str,
        active_slot_dev: str,
        refroot_mount_point: str,
        *,
        standby_as_ref: bool,
    ):
        """Mount reference rootfs that we copy files from to <refroot_mount_point>.

        This method bind mount refroot as ro with make-private flag and make-unbindable flag,
        to prevent ANY accidental writes/changes to the refroot.
        """
        _refroot_dev = standby_slot_dev if standby_as_ref else active_slot_dev

        # NOTE: set raise_exception to false to allow not mounted
        # not mounted dev will have empty return str
        if _refroot_active_mount_point := CMDHelperFuncs.get_mount_point_by_dev(
            _refroot_dev, raise_exception=False
        ):
            CMDHelperFuncs.bind_mount_ro(
                _refroot_active_mount_point,
                refroot_mount_point,
            )
        else:
            # NOTE: refroot is expected to be mounted,
            # if refroot is active rootfs, it is mounted as /;
            # if refroot is standby rootfs(in-place update mode),
            # it will be mounted to /mnt/standby(rw), so we still mount it as bind,ro
            raise MountError("refroot is expected to be mounted")

    @classmethod
    def reboot(cls):
        try:
            subprocess_call("reboot", raise_exception=True)
        except CalledProcessError:
            logger.exception("failed to reboot")
            raise


###### helper mixins ######
class SlotInUseMixin:
    current_ota_status_dir: Path
    standby_ota_status_dir: Path

    def _store_current_slot_in_use(self, _slot: str):
        write_to_file_sync(self.current_ota_status_dir / cfg.SLOT_IN_USE_FNAME, _slot)

    def _store_standby_slot_in_use(self, _slot: str):
        write_to_file_sync(self.standby_ota_status_dir / cfg.SLOT_IN_USE_FNAME, _slot)

    def _load_current_slot_in_use(self) -> Optional[str]:
        if res := read_from_file(
            self.current_ota_status_dir / cfg.SLOT_IN_USE_FNAME, default=""
        ):
            return res


class OTAStatusMixin:
    current_ota_status_dir: Path
    standby_ota_status_dir: Path
    ota_status: OTAStatusEnum

    def _store_current_ota_status(self, _status: OTAStatusEnum):
        write_to_file_sync(
            self.current_ota_status_dir / cfg.OTA_STATUS_FNAME, _status.name
        )

    def _store_standby_ota_status(self, _status: OTAStatusEnum):
        write_to_file_sync(
            self.standby_ota_status_dir / cfg.OTA_STATUS_FNAME, _status.name
        )

    def _load_current_ota_status(self) -> Optional[OTAStatusEnum]:
        if _status_str := read_from_file(
            self.current_ota_status_dir / cfg.OTA_STATUS_FNAME
        ).upper():
            try:
                return OTAStatusEnum[_status_str]
            except KeyError:
                pass  # invalid status string

    def get_ota_status(self) -> OTAStatusEnum:
        return self.ota_status


class VersionControlMixin:
    current_ota_status_dir: Path
    standby_ota_status_dir: Path

    def _store_standby_version(self, _version: str):
        write_to_file_sync(
            self.standby_ota_status_dir / cfg.OTA_VERSION_FNAME,
            _version,
        )

    def load_version(self) -> str:
        _version = read_from_file(
            self.current_ota_status_dir / cfg.OTA_VERSION_FNAME,
            missing_ok=True,
            default="",
        )
        if not _version:
            logger.warning("version file not found, return empty version string")

        return _version


class PrepareMountMixin:
    standby_slot_mount_point: Path
    ref_slot_mount_point: Path

    def _prepare_and_mount_standby(self, standby_slot_dev: str, *, erase=False):
        self.standby_slot_mount_point.mkdir(parents=True, exist_ok=True)

        # first try umount the dev
        CMDHelperFuncs.umount(standby_slot_dev)

        # format the whole standby slot if needed
        if erase:
            logger.warning(f"perform mkfs.ext4 on standby slot({standby_slot_dev})")
            CMDHelperFuncs.mkfs_ext4(standby_slot_dev)

        # try to mount the standby dev
        CMDHelperFuncs.mount_rw(standby_slot_dev, self.standby_slot_mount_point)

    def _mount_refroot(
        self,
        *,
        standby_dev: str,
        active_dev: str,
        standby_as_ref: bool,
    ):
        CMDHelperFuncs.mount_refroot(
            standby_slot_dev=standby_dev,
            active_slot_dev=active_dev,
            refroot_mount_point=str(self.ref_slot_mount_point),
            standby_as_ref=standby_as_ref,
        )

    def _umount_all(self, *, ignore_error: bool = False):
        CMDHelperFuncs.umount(self.standby_slot_mount_point, ignore_error=ignore_error)
        CMDHelperFuncs.umount(self.ref_slot_mount_point, ignore_error=ignore_error)
