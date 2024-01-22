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
from contextlib import contextmanager
from typing import Any, Callable, Literal, NoReturn, Optional
from typing_extensions import Concatenate

from otaclient._utils.subprocess import (
    compose_cmd,
    gen_err_report,
    subprocess_call,
    subprocess_check_output,
    SubProcessCallFailed,
    SubProcessCallTimeoutExpired,
)
from otaclient._utils.linux import DEFAULT_NS_TO_ENTER
from otaclient._utils.typing import ArgsType, StrOrPath, P, T
from otaclient._utils import truncate_str
from .configs import config as cfg
from .log_setting import get_logger

logger = get_logger(__name__)

# ------ thin wrappers for calling corresponding commands ------ #


@contextmanager
def log_subprocess_exec(
    _logger: Callable[[str], None], _msg: str = "", *, include_err_report: bool = True
):
    """A context manager that logs subprocess run failure.

    Exception is re-raised directly.
    """
    try:
        yield
    except (SubProcessCallFailed, SubProcessCallTimeoutExpired) as e:
        _err_report = gen_err_report(e) if include_err_report else ""
        _logger(f"{_msg}: {_err_report}")
        raise


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
def _findfs(_args: ArgsType, **kwargs) -> str | None:
    _cmd: list[str] = compose_cmd("findfs", _args)
    logger.debug(f"cmd execute: {' '.join(_cmd)}")
    return subprocess_check_output(_cmd, **kwargs)


@take_arg(subprocess_check_output)
def _findmnt(_args: ArgsType, **kwargs) -> str | None:
    _cmd: list[str] = compose_cmd("findmnt", _args)
    logger.debug(f"cmd execute: {' '.join(_cmd)}")
    return subprocess_check_output(_cmd, **kwargs)


@take_arg(subprocess_check_output)
def _lsblk(_args: ArgsType, **kwargs) -> str | None:
    _cmd: list[str] = compose_cmd("lsblk", _args)
    logger.debug(f"cmd execute: {' '.join(_cmd)}")
    return subprocess_check_output(_cmd, **kwargs)


@take_arg(subprocess_check_output)
def _lsof(_args: ArgsType, **kwargs) -> str | None:
    _cmd: list[str] = compose_cmd("lsof", _args)
    logger.debug(f"cmd execute: {' '.join(_cmd)}")
    return subprocess_check_output(_cmd, **kwargs)


@take_arg(subprocess_call)
def _mkfs_ext4(_args: ArgsType, **kwargs) -> None:
    _cmd: list[str] = compose_cmd("mkfs.ext4", _args)
    logger.debug(f"cmd execute: {' '.join(_cmd)}")
    return subprocess_call(_cmd, **kwargs)


@take_arg(subprocess_call)
def _reboot(_args: ArgsType, **kwargs) -> None:
    _cmd: list[str] = compose_cmd("reboot", _args)
    logger.debug(f"cmd execute: {' '.join(_cmd)}")
    return subprocess_call(_cmd, **kwargs)


@take_arg(subprocess_call)
def _mount(_args: ArgsType, **kwargs) -> None:
    _cmd: list[str] = compose_cmd("mount", _args)
    logger.debug(f"cmd execute: {' '.join(_cmd)}")
    return subprocess_call(_cmd, **kwargs)


@take_arg(subprocess_call)
def _umount(_args: ArgsType, **kwargs) -> None:
    _cmd: list[str] = compose_cmd("umount", _args)
    logger.debug(f"cmd execute: {' '.join(_cmd)}")
    return subprocess_call(_cmd, **kwargs)


@take_arg(subprocess_call)
def _e2label(_args: ArgsType, **kwargs) -> None:
    _cmd: list[str] = compose_cmd("e2label", _args)
    logger.debug(f"cmd execute: {' '.join(_cmd)}")
    return subprocess_call(_cmd, **kwargs)


# ------ concrete helpers for specific purpose ------ #

DEV_ATTRS = ("PARTUUID", "UUID", "LABEL", "PARTLABEL")
DEV_ATTRS_LITERAL = Literal["PARTUUID", "UUID", "LABEL", "PARTLABEL"]


def reboot(_args: str = "") -> NoReturn:
    """Reboot the whole system otaclient running at and terminate otaclient.

    NOTE: rpi_boot's reboot takes args.
    NOTE(20231205): if reboot command succeeded, this function must terminates otaclient.
    NOTE(20240118): this command requires nsenter to root ns.
    """
    # if in container mode, execute reboot on host ns
    with log_subprocess_exec(logger.error, f"failed to reboot({_args=}) the system"):
        _reboot(
            _args,
            raise_exception=True,
            enter_root_ns=DEFAULT_NS_TO_ENTER if cfg.IS_CONTAINER else None,
        )
        sys.exit(0)  # must ensure otaclient exits after reboot is called


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


# more specific version of get_attr_from_dev
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
    """Check if target device is mounted, or target folder is used as mount point.

    NOTE(20240118): this command requires nsenter to root ns.
    """
    _mount_info = _findmnt(
        [str(target)],
        enter_root_ns=DEFAULT_NS_TO_ENTER if cfg.IS_CONTAINER else None,
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
        SubprocessCallFailed on failed ext4 partition formatting.
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
    with log_subprocess_exec(logger.error, f"failed to format {dev} using mkfs.ext4"):
        _args = [*specify_fsuuid, *specify_fslabel, str(dev)]
        _mkfs_ext4(_args, raise_exception=True, timeout=timeout)


#
# ------ mount related helper funcs and exceptions ------ #
#
# NOTE(20240118): always execute mount/umount in the root mnt namespace,
#                 mount points created by otaclient should be in root mnt namespace.

DEFAULT_MOUNT_TIMEOUT = None
DEFAULT_UMOUNT_TIMEOUT = None


def mount(
    dev: StrOrPath,
    mount_point: StrOrPath,
    *,
    options: Optional[list[str]] = None,
    args: Optional[list[str]] = None,
    timeout: Optional[float] = DEFAULT_MOUNT_TIMEOUT,
) -> None:
    """mount [-o option1[,option2, ...]]] [args[0] [args[1]...]] <dev> <mount_point>

    NOTE(20240118): always executed in root mount ns.

    Raises:
        SubprocessCallFailed on failed mounting, or SubProcessCallTimeoutExpired on timeout mount.
    """
    _options = ["-o", ",".join(options)] if isinstance(options, list) else []
    _args = args if isinstance(args, list) else []

    _mount_params = [*_options, *_args, str(dev), str(mount_point)]
    _mount(
        _mount_params,
        enter_root_ns=DEFAULT_NS_TO_ENTER if cfg.IS_CONTAINER else None,
        raise_exception=True,
        timeout=timeout,
    )


def umount(
    target: StrOrPath,
    *,
    recursive: bool = False,
    all_mounts: bool = False,
    list_opened_files: bool = True,
    timeout: Optional[float] = DEFAULT_UMOUNT_TIMEOUT,
) -> None:
    """Umount target with options.

    Raises:
        SubprocessCallFailed on failed mounting, or SubProcessCallTimeoutExpired on timeout mount.
    """
    _args = []
    if recursive:
        _args.append("-R")
    if all_mounts:
        _args.append("-A")
    _args.append(str(target))

    try:
        _umount(
            _args,
            enter_root_ns=DEFAULT_NS_TO_ENTER if cfg.IS_CONTAINER else None,
            raise_exception=True,
            timeout=timeout,
        )
    except SubProcessCallFailed as e:
        _std_err: str = e.stderr.decode()
        if _std_err.find("not mounted") != -1:
            return

        if list_opened_files and _std_err.find("target is busy") != -1:
            _opened_files = truncate_str(get_opened_files_on_target(target) or "", 2048)
            logger.error(f"opened files on {target}: \n{_opened_files}")
        raise


def mount_rw(
    target_dev: StrOrPath,
    mount_point: StrOrPath,
    *,
    timeout: Optional[float] = DEFAULT_MOUNT_TIMEOUT,
) -> None:
    """Mount the target to the mount_point read-write exclusively.

    NOTE: pass args = ["--make-unbindable"] to prevent re-bind of this mount point.
    NOTE(20231205): although linux kernel allows multiple direct mount on
            the same device, the mount options can not be changed.
            Second mount to the same device with different -o will cause "already mounted" error.
    NOTE(20231205): mount point itself can be stacked, new one will override the old one.
    NOTE(20240118): always mount in root namespace.

    Raises:
        SubprocessCallFailed on failed mounting, or SubProcessCallTimeoutExpired on timeout mount.
    """
    # first try to unconditionally umount all mount points on this target(device)
    with log_subprocess_exec(
        logger.error, f"failed to unconditionally umount {target_dev}"
    ):
        umount(target_dev)

    with log_subprocess_exec(
        logger.error, f"failed to mount {target_dev} to {mount_point}"
    ):
        mount(
            target_dev,
            mount_point,
            options=["rw"],
            args=["--make-unbindable"],
            timeout=timeout,
        )


def bind_mount_ro(
    target_mp: StrOrPath,
    mount_point: StrOrPath,
    *,
    timeout: Optional[float] = None,
) -> None:
    """Bind mount the target to the mount_point read-only.

    Raises:
        SubprocessCallFailed on failed mounting, or SubProcessCallTimeoutExpired on timeout mount.
    """
    with log_subprocess_exec(
        logger.error, f"failed to bind mount ro {target_mp} to {mount_point}"
    ):
        mount(
            target_mp,
            mount_point,
            options=["bind", "ro"],
            args=["--make-unbindable"],
            timeout=timeout,
        )


def mount_ro(
    target_dev: StrOrPath,
    mount_point: StrOrPath,
    *,
    timeout: Optional[float] = DEFAULT_MOUNT_TIMEOUT,
) -> None:
    """Mount target on mount_point read-only exclusively.

    Raises:
        SubprocessCallFailed on failed mounting, or SubProcessCallTimeoutExpired on timeout mount.
    """
    with log_subprocess_exec(
        logger.error, f"failed to unconditionally umount {target_dev}"
    ):
        umount(target_dev)

    with log_subprocess_exec(
        logger.error, f"failed to mount ro {target_dev} to {mount_point}"
    ):
        mount(
            target_dev,
            mount_point,
            options=["ro"],
            args=["--make-unbindable"],
            timeout=timeout,
        )


def get_opened_files_on_target(target: StrOrPath, *, timeout: float = 3) -> str | None:
    """Get opened files on specific target.

    NOTE: this function is executed in root mount ns.
    """
    return _lsof(
        [str(target)],
        enter_root_ns=DEFAULT_NS_TO_ENTER if cfg.IS_CONTAINER else None,
        raise_exception=False,
        timeout=timeout,
    )
