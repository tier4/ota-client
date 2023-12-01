"""Helper functions for calling common used commands in subprocess."""


from __future__ import annotations
import functools
import sys
from enum import Enum, unique
from typing import Any, Callable, Literal, NoReturn, Optional, TypeVar
from typing_extensions import Concatenate, ParamSpec

from otaclient._utils.subprocess import (
    SubProcessCalledFailed,
    subprocess_call,
    subprocess_check_output,
)
from otaclient._utils.typing import StrOrPath
from ..configs import config as cfg
from ..log_setting import get_logger

logger = get_logger(__name__)

P = ParamSpec("P")
T = TypeVar("T")

# ------ thin wrappers for calling corresponding commands ------ #


def log_exc(err_handler: Callable[[str], None]):
    """A wrapper that handles logging when execution failed."""

    def _decorator(_target: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(_target)
        def _inner(*args, **kwargs):
            try:
                _target(*args, **kwargs)
            except SubProcessCalledFailed as e:
                err_handler(e.err_report)
                raise

        return _inner  # type: ignore

    return _decorator


def take_arg(_: Callable[Concatenate[str, P], Any]):
    """Typehints cmdhelper that takes input args."""

    def _decorator(_func: Callable[..., T]) -> Callable[Concatenate[str, P], T]:
        return _func  # type: ignore

    return _decorator


def no_arg(_: Callable[Concatenate[str, P], Any]):
    """Typehints cmdhelper that takes no input args."""

    def _decorator(_func: Callable[..., T]) -> Callable[P, T]:
        return _func  # type: ignore

    return _decorator


@take_arg(subprocess_check_output)
@log_exc(logger.warning)
def _findfs(_args: str, **kwargs) -> str | None:
    return subprocess_check_output(f"findfs {_args}", **kwargs)


@take_arg(subprocess_check_output)
@log_exc(logger.warning)
def _findmnt(_args: str, **kwargs) -> str | None:
    return subprocess_check_output(f"findmnt {_args}", **kwargs)


@take_arg(subprocess_check_output)
@log_exc(logger.error)
def _lsblk(_args: str, **kwargs) -> str | None:
    return subprocess_check_output(f"lsblk {_args}", **kwargs)


@take_arg(subprocess_check_output)
@log_exc(logger.error)
def _mkfs_ext4(_args: str, **kwargs) -> None:
    return subprocess_call(f"mkfs.ext4 {_args}", **kwargs)


@take_arg(subprocess_check_output)
@log_exc(logger.error)
def _reboot(_args: str, **kwargs) -> None:
    return subprocess_call(f"reboot {_args}", **kwargs)


@take_arg(subprocess_check_output)
@log_exc(logger.error)
def _mount(_args: str, **kwargs) -> None:
    return subprocess_call(f"mount {_args}", **kwargs)


@take_arg(subprocess_check_output)
@log_exc(logger.warning)
def _umount(_args: str, **kwargs) -> None:
    return subprocess_call(f"umount {_args}", **kwargs)


@take_arg(subprocess_check_output)
@log_exc(logger.error)
def _e2label(_args: str, **kwargs) -> None:
    return subprocess_call(f"e2label {_args}", **kwargs)


# ------ concrete helpers for specific purpose ------ #

DEV_ATTRS = ("PARTUUID", "UUID", "LABEL", "PARTLABEL")
DEV_ATTRS_LITERAL = Literal["PARTUUID", "UUID", "LABEL", "PARTLABEL"]


def reboot(_args: str = "") -> NoReturn:
    """Reboot the whole system otaclient running at and terminate otaclient.

    NOTE: rpi_boot's reboot takes args.
    NOTE(20230614): this command MUST also make otaclient exit immediately.
    """
    # if in container mode, execute reboot on host ns
    new_root = cfg.ACTIVE_ROOTFS if cfg.IS_CONTAINER else None

    try:
        _reboot(_args, raise_exception=True, new_root=new_root)
    except Exception:
        logger.error("failed to reboot the system")
        raise
    finally:  # ensure otaclient exits on this function being called
        sys.exit(0)


def get_attr_from_dev(
    dev: StrOrPath,
    attr_n: DEV_ATTRS_LITERAL,
    *,
    raise_exception: bool,
    timeout: Optional[float] = None,
) -> str | None:
    """Use lsblk to query dev attr value."""
    _args = f"-in -o {attr_n} {dev}"
    return _lsblk(_args, timeout=timeout, raise_exception=raise_exception)


def get_uuid_str(uuid: str) -> str:
    """Return UUID string in "UUID=<uuid>" format."""
    return f"UUID={uuid}"


def get_partuuid_str(partuuid: str) -> str:
    """Return PARTUUID string in "PARTUUID=<partuuid>" format."""
    return f"PARTUUID={partuuid}"


def get_dev_by_attr(
    attr_name: DEV_ATTRS_LITERAL,
    attr_value: str,
    *,
    raise_exception: bool,
    timeout: Optional[float] = None,
) -> str | None:
    """Search dev by <attr> with lsblk and return its full dev path."""
    _args = f"{attr_name}={attr_value}"
    return _findfs(_args, timeout=timeout, raise_exception=raise_exception)


def get_current_rootfs_dev(
    *,
    raise_exception: bool,
    timeout: Optional[float] = None,
) -> str | None:
    """Get current rootfs dev with findmnt.
    NOTE:
        -o <COLUMN>: only print <COLUMN>
        -n: no headings
        -f: only show the first file system
        -c: canonicalize printed paths

    Returns:
        full path to dev of the current rootfs
    """
    _active_rootfs = cfg.ACTIVE_ROOTFS
    return _findmnt(
        f"{_active_rootfs} -o SOURCE -n -f -c",
        timeout=timeout,
        raise_exception=raise_exception,
    )


def get_dev_by_mount_point(
    mount_point: StrOrPath,
    *,
    raise_exception: bool,
    timeout: Optional[float] = None,
) -> str | None:
    """Return the underlying mounted dev of the given mount_point."""
    return _findmnt(
        f"-no SOURCE {mount_point}", timeout=timeout, raise_exception=raise_exception
    )


def get_dev_tree(
    _parent: StrOrPath,
    *,
    raise_exception: bool,
    timeout: Optional[float] = None,
) -> str | None:
    """
    If <_parent> is "/dev/sdx", example return value is:

        NAME="/dev/sdx"
        NAME="/dev/sdx1" # system-boot
        NAME="/dev/sdx2" # slot_a
        NAME="/dev/sdx3" # slot_b

    """
    _args = f"-Pp -o NAME {_parent}"
    return _lsblk(_args, timeout=timeout, raise_exception=raise_exception)


def is_target_mounted(
    target: StrOrPath,
    *,
    raise_exception: bool,
    timeout: Optional[float] = None,
) -> bool:
    """Check if target device is mounted, or target folder is used as mount point."""
    _mount_info = _findmnt(
        f"{target}",
        timeout=timeout,
        raise_exception=raise_exception,
    )
    return bool(_mount_info)


def get_parent_dev(
    child_device: StrOrPath,
    *,
    raise_exception: bool,
    timeout: Optional[float] = None,
) -> str | None:
    """
    When `/dev/nvme0n1p1` is specified as child_device, /dev/nvme0n1 is returned.

    cmd params:
        -d: print the result from specified dev only.
    """
    _args = f"-idpn -o PKNAME {child_device}"
    return _lsblk(_args, timeout=timeout, raise_exception=raise_exception)


def set_dev_fslabel(
    dev: StrOrPath,
    fslabel: str,
    *,
    raise_exception: bool = True,
    timeout: Optional[float] = None,
) -> None:
    _args = f"{dev} {fslabel}"
    _e2label(_args, timeout=timeout, raise_exception=raise_exception)


def mkfs_ext4(
    dev: StrOrPath,
    fslabel: Optional[str] = None,
    fsuuid: Optional[str] = None,
    *,
    preserve_fslabel: bool,
    preserve_fsuuid: bool,
    timeout: Optional[float] = None,
) -> None:
    """Call mkfs.ext4 on <dev>.

    Args:
        dev (StrOrPath): the target partition to format as ext4.
        fslabel (str = None): fslabel to assign.
        fsuuid (str = None): fsuuid to assign.
        preserve_fslabel (bool): whether to preserve previous fs' fslabel
            if available. If set to True, <fslabel> param will be ignored.
        preserve_fsuuid (bool): whether to preserve previous fs'fsuuid
            if available. If set to True, <fsuuid> param will be ignored.

    Raises:
        MkfsError on failed ext4 partition formatting.
    """
    if preserve_fslabel and (
        _prev_fslabel := get_attr_from_dev(dev, "LABEL", raise_exception=False)
    ):
        fslabel = _prev_fslabel
    specify_fslabel = f"-L {fslabel}" if fslabel else ""

    if preserve_fsuuid and (
        _prev_fsuuid := get_attr_from_dev(dev, "UUID", raise_exception=False)
    ):
        fsuuid = _prev_fsuuid
    specify_fsuuid = f"-U {fsuuid}" if fsuuid else ""

    logger.warning(f"format {dev} to ext4...")
    try:
        _args = f"{specify_fsuuid} {specify_fslabel} {dev}"
        _mkfs_ext4(_args, raise_exception=True, timeout=timeout)
    except SubProcessCalledFailed as e:
        logger.error(f"failed to format {dev} as mkfs.ext4 on: {e!r}")
        raise


#
# ------ mount related helper funcs and exceptions ------ #
#


class MountError(Exception):
    """Mount operation failure related exception."""

    def __init__(self, *args: object, failure_reason: MountFailedReason) -> None:
        self.failure_reason = failure_reason
        super().__init__(*args)


@unique
class MountFailedReason(int, Enum):
    # error code
    SUCCESS = 0
    PERMISSIONS_ERROR = 1
    SYSTEM_ERROR = 2
    INTERNAL_ERROR = 4
    USER_INTERRUPT = 8
    GENERIC_MOUNT_FAILURE = 32

    # custom error code
    # specific reason for generic mount failure
    TARGET_NOT_FOUND = -1
    TARGET_ALREADY_MOUNTED = -2
    MOUNT_POINT_NOT_FOUND = -3
    BIND_MOUNT_ON_NON_DIR = -4


def _parse_mount_failure(func: Callable[..., Any]):
    @functools.wraps(func)
    def _wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except SubProcessCalledFailed as e:
            _reason = MountFailedReason(e.return_code)

            if _reason != MountFailedReason.GENERIC_MOUNT_FAILURE:
                raise MountError(failure_reason=_reason)

            # if the return code is 32, determine the detailed reason
            # of the mount failure
            _error_msg = str(e.stderr)
            if _error_msg.find("already mounted") != -1:
                _fail_reason = MountFailedReason.TARGET_ALREADY_MOUNTED
            elif _error_msg.find("mount point does not exist") != -1:
                _fail_reason = MountFailedReason.MOUNT_POINT_NOT_FOUND
            elif _error_msg.find("does not exist") != -1:
                _fail_reason = MountFailedReason.TARGET_NOT_FOUND
            elif _error_msg.find("Not a directory") != -1:
                _fail_reason = MountFailedReason.BIND_MOUNT_ON_NON_DIR
            else:
                _fail_reason = MountFailedReason.GENERIC_MOUNT_FAILURE

            raise MountError(_error_msg, failure_reason=_fail_reason)

    return _wrapper


@_parse_mount_failure
def mount(
    dev: StrOrPath,
    mount_point: StrOrPath,
    *,
    options: Optional[list[str]] = None,
    args: Optional[list[str]] = None,
    timeout: Optional[float] = None,
) -> None:
    """
    mount [-o option1[,option2, ...]]] [args[0] [args[1]...]] <dev> <mount_point>

    Raises:
        MountError on failed mounting.
    """
    _option_str = f"-o {','.join(options)}" if isinstance(options, list) else ""
    _args_str = f"{' '.join(args)}" if isinstance(args, list) else ""

    _args = f"{_option_str} {_args_str} {dev} {mount_point}"
    _mount(_args, raise_exception=True, timeout=timeout)


def mount_rw(
    target: StrOrPath,
    mount_point: StrOrPath,
    *,
    raise_exception: bool,
    timeout: Optional[float] = None,
) -> None:
    """Mount the target to the mount_point read-write.

    NOTE: pass args = ["--make-private", "--make-unbindable"] to prevent
            mount events propagation to/from this mount point.

    Raises:
        MountError on failed mounting.
    """
    try:
        mount(
            target,
            mount_point,
            options=["rw"],
            args=["--make-private", "--make-unbindable"],
            timeout=timeout,
        )
    except MountError as e:
        logger.error(f"failed to mount {target=} to {mount_point=}: {e!r}")
        if raise_exception:
            raise


def bind_mount_ro(
    target: StrOrPath,
    mount_point: StrOrPath,
    *,
    raise_exception: bool,
    timeout: Optional[float] = None,
) -> None:
    """Bind mount the target to the mount_point read-only.

    NOTE: pass args = ["--make-private", "--make-unbindable"] to prevent
            mount events propagation to/from this mount point.

    Raises:
        MountError on failed mounting.
    """
    try:
        mount(
            target,
            mount_point,
            options=["bind", "ro"],
            args=["--make-private", "--make-unbindable"],
            timeout=timeout,
        )
    except MountError as e:
        logger.error(f"failed to bind_mount_ro {target=} to {mount_point=}: {e!r}")
        if raise_exception:
            raise


def mount_ro(
    target: StrOrPath,
    mount_point: StrOrPath,
    *,
    raise_exception: bool,
    timeout: Optional[float] = None,
) -> None:
    """Mount target on mount_point read-only.

    If the target device is mounted, we bind mount the target device to mount_point,
    if the target device is not mounted, we directly mount it to the mount_point.

    This method mount the target as ro with make-private flag and make-unbindable flag,
    to prevent ANY accidental writes/changes to the target.

    Raises:
        MountError on failed mounting.
    """
    if is_target_mounted(target, raise_exception=False):
        return bind_mount_ro(
            target,
            mount_point,
            timeout=timeout,
            raise_exception=raise_exception,
        )

    try:
        mount(
            target,
            mount_point,
            options=["ro"],
            args=["--make-private", "--make-unbindable"],
            timeout=timeout,
            raise_exception=raise_exception,
        )
    except MountError as e:
        logger.error(f"failed to mount_ro {target=} to {mount_point=}: {e!r}")
        if raise_exception:
            raise


def umount(
    target: StrOrPath,
    *,
    raise_exception: bool = False,
    timeout: Optional[float] = None,
) -> None:
    """Try to unmount the <target>."""
    if not is_target_mounted(target, raise_exception=False):
        return

    try:
        _args = f"-l {target}"
        _umount(_args, raise_exception=True, timeout=timeout)
    except SubProcessCalledFailed as e:
        logger.warning(f"failed to unmount {target}: {e!r}")
        if raise_exception:
            raise
