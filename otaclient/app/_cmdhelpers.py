# Copyright 2022 TIER IV, INC. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Helper functions for calling common used commands in subprocess."""


from __future__ import annotations
import functools
import sys
from enum import Enum, unique
from typing import Any, Callable, Literal, NoReturn, Optional
from typing_extensions import Concatenate

from otaclient._utils.subprocess import (
    compose_cmd,
    subprocess_call,
    subprocess_check_output,
    SubProcessCalledFailed,
)
from otaclient._utils.typing import ArgsType, StrOrPath, P, T
from otaclient._utils import truncate_str_or_bytes
from .configs import config as cfg
from .log_setting import get_logger

logger = get_logger(__name__)

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


def take_arg(_: Callable[Concatenate[Any, P], Any]):
    """Typehints cmdhelper that takes input args."""

    def _decorator(_func: Callable[..., T]) -> Callable[Concatenate[ArgsType, P], T]:
        return _func  # type: ignore

    return _decorator


def no_arg(_: Callable[Concatenate[Any, P], Any]):
    """Typehints cmdhelper that takes no input args."""

    def _decorator(_func: Callable[..., T]) -> Callable[P, T]:
        return _func  # type: ignore

    return _decorator


@take_arg(subprocess_check_output)
@log_exc(logger.warning)
def _findfs(_args: ArgsType, **kwargs) -> str | None:
    _cmd: list[str] = compose_cmd("findfs", _args)
    return subprocess_check_output(_cmd, **kwargs)


@take_arg(subprocess_check_output)
@log_exc(logger.warning)
def _findmnt(_args: ArgsType, **kwargs) -> str | None:
    _cmd: list[str] = compose_cmd("findmnt", _args)
    return subprocess_check_output(_cmd, **kwargs)


@take_arg(subprocess_check_output)
@log_exc(logger.error)
def _lsblk(_args: ArgsType, **kwargs) -> str | None:
    _cmd: list[str] = compose_cmd("lsblk", _args)
    return subprocess_check_output(_cmd, **kwargs)


@take_arg(subprocess_check_output)
@log_exc(logger.debug)
def _lsof(_args: ArgsType, **kwargs) -> str | None:
    _cmd: list[str] = compose_cmd("lsof", _args)
    return subprocess_check_output(_cmd, **kwargs)


@take_arg(subprocess_call)
@log_exc(logger.error)
def _mkfs_ext4(_args: ArgsType, **kwargs) -> None:
    _cmd: list[str] = compose_cmd("mkfs.ext4", _args)
    return subprocess_call(_cmd, **kwargs)


@take_arg(subprocess_call)
@log_exc(logger.error)
def _reboot(_args: ArgsType, **kwargs) -> None:
    _cmd: list[str] = compose_cmd("reboot", _args)
    return subprocess_call(_cmd, **kwargs)


@take_arg(subprocess_call)
@log_exc(logger.error)
def _mount(_args: ArgsType, **kwargs) -> None:
    _cmd: list[str] = compose_cmd("mount", _args)
    return subprocess_call(_cmd, **kwargs)


@take_arg(subprocess_call)
@log_exc(logger.warning)
def _umount(_args: ArgsType, **kwargs) -> None:
    _cmd: list[str] = compose_cmd("umount", _args)
    return subprocess_call(_cmd, **kwargs)


@take_arg(subprocess_call)
@log_exc(logger.error)
def _e2label(_args: ArgsType, **kwargs) -> None:
    _cmd: list[str] = compose_cmd("e2label", _args)
    return subprocess_call(_cmd, **kwargs)


# ------ concrete helpers for specific purpose ------ #

DEV_ATTRS = ("PARTUUID", "UUID", "LABEL", "PARTLABEL")
DEV_ATTRS_LITERAL = Literal["PARTUUID", "UUID", "LABEL", "PARTLABEL"]


def reboot(_args: str = "") -> NoReturn:
    """Reboot the whole system otaclient running at and terminate otaclient.

    NOTE: rpi_boot's reboot takes args.
    NOTE(20231205): if reboot command succeeded, this function must terminates otaclient.
    """
    # if in container mode, execute reboot on host ns
    new_root = cfg.ACTIVE_ROOTFS if cfg.IS_CONTAINER else None

    try:
        _reboot(_args, raise_exception=True, new_root=new_root)
        sys.exit(0)
    except SubProcessCalledFailed:
        logger.error(f"failed to reboot({_args=}) the system")
        raise


def get_attr_from_dev(
    dev: StrOrPath,
    attr_n: DEV_ATTRS_LITERAL,
    *,
    raise_exception: bool,
    timeout: Optional[float] = None,
) -> str | None:
    """Use lsblk to query dev attr value."""
    _args = ["-in", "-o", attr_n, str(dev)]
    return _lsblk(_args, timeout=timeout, raise_exception=raise_exception)


get_dev_fsuuid = functools.partial(get_attr_from_dev, attr_n="UUID")
get_dev_partuuid = functools.partial(get_attr_from_dev, attr_n="PARTUUID")
get_dev_fslabel = functools.partial(get_attr_from_dev, attr_n="LABEL")


def gen_uuid_str(uuid: str) -> str:
    """Return UUID string in "UUID=<uuid>" format."""
    return f"UUID={uuid}"


def gen_partuuid_str(partuuid: str) -> str:
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
    _args = [f"{attr_name}={attr_value}"]
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
        ["-nfco", "SOURCE", str(_active_rootfs)],
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
        ["-no", "SOURCE", str(mount_point)],
        timeout=timeout,
        raise_exception=raise_exception,
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
    _args = ["-Ppo", "NAME", str(_parent)]
    return _lsblk(_args, timeout=timeout, raise_exception=raise_exception)


def is_target_mounted(
    target: StrOrPath,
    *,
    raise_exception: bool,
    timeout: Optional[float] = None,
) -> bool:
    """Check if target device is mounted, or target folder is used as mount point."""
    _mount_info = _findmnt(
        [str(target)],
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
    _args = ["-idpno", "PKNAME", str(child_device)]
    return _lsblk(_args, timeout=timeout, raise_exception=raise_exception)


def set_ext4_dev_fslabel(
    dev: StrOrPath,
    fslabel: str,
    *,
    raise_exception: bool = True,
    timeout: Optional[float] = None,
) -> None:
    _args = [str(dev), fslabel]
    _e2label(_args, timeout=timeout, raise_exception=raise_exception)


def mkfs_ext4(
    dev: StrOrPath,
    fslabel: Optional[str] = None,
    fsuuid: Optional[str] = None,
    *,
    preserve_fslabel: bool = False,
    preserve_fsuuid: bool = False,
    timeout: Optional[float] = None,
) -> None:
    """Call mkfs.ext4 on <dev>.

    Args:
        dev (StrOrPath): the target partition to format as ext4.
        fslabel (str = None): fslabel to assign.
        fsuuid (str = None): fsuuid to assign.
        preserve_fslabel (bool = False): whether to preserve previous fs' fslabel
            if available. If set to True, <fslabel> param will be ignored.
        preserve_fsuuid (bool = False): whether to preserve previous fs'fsuuid
            if available. If set to True, <fsuuid> param will be ignored.

    Raises:
        MkfsError on failed ext4 partition formatting.
    """
    if preserve_fslabel and (
        _prev_fslabel := get_attr_from_dev(dev, "LABEL", raise_exception=False)
    ):
        fslabel = _prev_fslabel
    specify_fslabel = ["-L", fslabel] if fslabel else []

    if preserve_fsuuid and (
        _prev_fsuuid := get_attr_from_dev(dev, "UUID", raise_exception=False)
    ):
        fsuuid = _prev_fsuuid
    specify_fsuuid = ["-U", fsuuid] if fsuuid else []

    logger.warning(f"format {dev} to ext4({fsuuid=}, {fslabel=})...")
    try:
        _args = [*specify_fsuuid, *specify_fslabel, str(dev)]
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
    TARGET_IS_BUSY = -5


def _parse_mount_failure(func: Callable[P, T]) -> Callable[P, T]:
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
            elif _error_msg.find("target is busy") != -1:
                _fail_reason = MountFailedReason.TARGET_IS_BUSY
            else:
                _fail_reason = MountFailedReason.GENERIC_MOUNT_FAILURE

            raise MountError(_error_msg, failure_reason=_fail_reason)

    return _wrapper  # type: ignore


@_parse_mount_failure
def mount(
    dev: StrOrPath,
    mount_point: StrOrPath,
    *,
    options: Optional[list[str]] = None,
    args: Optional[list[str]] = None,
    timeout: Optional[float] = None,
) -> None:
    """mount [-o option1[,option2, ...]]] [args[0] [args[1]...]] <dev> <mount_point>

    Raises:
        MountError on failed mounting.
    """
    _options = ["-o", *options] if isinstance(options, list) else []
    _args = args if isinstance(args, list) else []

    _mount_params = [*_options, *_args, str(dev), str(mount_point)]
    _mount(_mount_params, raise_exception=True, timeout=timeout)


@_parse_mount_failure
def umount(
    target: StrOrPath,
    *,
    args: Optional[list[str]] = None,
    timeout: Optional[float] = None,
):
    """umount [args[0] [args[1]...]] <target>

    Raises:
        MountError on failed umounting.
    """
    _args = args if isinstance(args, list) else []

    _umount_params = [*_args, str(target)]
    _umount(_umount_params, raise_exception=True, timeout=timeout)


def mount_rw(
    target: StrOrPath,
    mount_point: StrOrPath,
    *,
    raise_exception: bool,
    timeout: Optional[float] = None,
) -> None:
    """Mount the target to the mount_point read-write.

    We will try to exclusively mount the target drive.

    NOTE: pass args = ["--make-private", "--make-unbindable"] to prevent
            mount events propagation to/from this mount point.
    NOTE(20231205): although linux kernel allows multiple direct mount on
            the same device, the mount options can not be changed.
            Second mount to the same device with different -o will cause "already mounted" error.
    NOTE(20231205): mount point itself can be stacked, new one will override the old one.

    Raises:
        MountError on failed mounting.
    """
    _exec_mount = functools.partial(
        mount,
        target,
        mount_point,
        options=["rw"],
        args=["--make-private", "--make-unbindable"],
        timeout=timeout,
    )

    # try mount first
    _mount_exception: Optional[MountError] = None
    try:
        return _exec_mount()
    except MountError as e:
        _mount_exception = e  # get exc from exception handling namespace

    # first mount failed with other reason rather than "already mounted" error
    if _mount_exception.failure_reason != MountFailedReason.TARGET_ALREADY_MOUNTED:
        _err_msg = (
            f"failed to mount {target=} to {mount_point=} r/w: {_mount_exception!r}"
        )
        logger.error(_err_msg)
        if raise_exception:
            try:
                raise _mount_exception from None
            finally:
                _mount_exception = None  # break cyclic ref
        return

    # target is already mounted try to umount first and then try mount again
    try:
        umount_target(target, raise_exception=True)
        _exec_mount()
    except MountError as e:
        _err_msg = f"try to umount {target} and mount r/w again to {mount_point=} failed: {e!r}"
        logger.error(_err_msg)
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
            # --make-private: prevent receive/propagate mount out
            # --make-unbindable: prevent this mount point being bind mount by others
            args=["--make-private", "--make-unbindable"],
            timeout=timeout,
        )
    except MountError as e:
        logger.error(f"failed to mount_ro {target=} to {mount_point=}: {e!r}")
        if raise_exception:
            raise


_MAX_LSOF_OUTPUT_LEN = 2048


def umount_target(
    target: StrOrPath,
    *,
    recursive: bool = False,
    raise_exception: bool = False,
    list_opened_files: bool = False,
    list_opened_files_timeout: float = 3,
    timeout: Optional[float] = None,
) -> None:
    """Try to unmount the <target>.

    Args:
        list_opened_files(bool = False): if True and umount failure reason is
            "target is busy", list opened files on the target device.

    Raises:
        MountError with reason=GENERIC_MOUNT_FAILURE when umount failed.
    """
    if not is_target_mounted(target, raise_exception=False):
        return  # no need to try umount a not mounted target

    _args = []
    if recursive:
        _args.append("-R")

    try:
        umount(target, args=_args, timeout=timeout)
    except MountError as e:
        _err_msg = f"failed to unmount {target}: {e!r}"
        logger.warning(_err_msg)

        if (
            e.failure_reason == MountFailedReason.TARGET_IS_BUSY
            and list_opened_files
            and (
                _open_files := _lsof(
                    [str(target)],
                    raise_exception=False,
                    timeout=list_opened_files_timeout,
                )
            )
        ):
            _open_files = truncate_str_or_bytes(_open_files, _MAX_LSOF_OUTPUT_LEN)
            logger.warning(f"open files on {target=}: \n{_open_files}")

        if raise_exception:
            raise MountError(
                _err_msg, failure_reason=MountFailedReason.GENERIC_MOUNT_FAILURE
            ) from e
