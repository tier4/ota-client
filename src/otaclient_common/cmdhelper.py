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
"""Subprocess call collections for otaclient use.

When underlying subprocess call failed and <raise_exception> is True,
    functions defined in this class will raise the original CalledProcessError
    to the upper caller.
"""


from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from subprocess import CalledProcessError
from typing import Literal, NoReturn, Protocol

from otaclient_common.common import subprocess_call, subprocess_check_output
from otaclient_common.typing import StrOrPath

logger = logging.getLogger(__name__)

# fmt: off
PartitionToken = Literal[
    "UUID", "PARTUUID",
    "LABEL", "PARTLABEL",
    "TYPE",
]
# fmt: on


def get_attrs_by_dev(
    attr: PartitionToken, dev: StrOrPath, *, raise_exception: bool = True
) -> str:  # pragma: no cover
    """Get <attr> from <dev>.

    This is implemented by calling:
        `lsblk -in -o <attr> <dev>`

    Args:
        attr (PartitionToken): the attribute to retrieve from the <dev>.
        dev (StrOrPath): the target device path.
        raise_exception (bool, optional): raise exception on subprocess call failed.
            Defaults to True.

    Returns:
        str: <attr> of <dev>.
    """
    cmd = ["lsblk", "-ino", attr, str(dev)]
    return subprocess_check_output(cmd, raise_exception=raise_exception)


def get_dev_by_token(
    token: PartitionToken, value: str, *, raise_exception: bool = True
) -> list[str] | None:  # pragma: no cover
    """Get a list of device(s) that matches the <token>=<value> pair.

    This is implemented by calling:
        blkid -o device -t <TOKEN>=<VALUE>

    Args:
        token (PartitionToken): which attribute of device to match.
        value (str): the value of the attribute.
        raise_exception (bool, optional): raise exception on subprocess call failed.
            Defaults to True.

    Returns:
        Optional[list[str]]: If there is at least one device found, return a list
            contains all found device(s), otherwise None.
    """
    cmd = ["blkid", "-o", "device", "-t", f"{token}={value}"]
    if res := subprocess_check_output(cmd, raise_exception=raise_exception):
        return res.splitlines()


def get_current_rootfs_dev(
    active_root: StrOrPath, *, raise_exception: bool = True
) -> str:  # pragma: no cover
    """Get the devpath of current rootfs dev.

    This is implemented by calling
        findmnt -nfc -o SOURCE <ACTIVE_ROOTFS_PATH>

    Args:
        raise_exception (bool, optional): raise exception on subprocess call failed.
            Defaults to True.

    Returns:
        str: the devpath of current rootfs device.
    """
    cmd = ["findmnt", "-nfco", "SOURCE", active_root]
    return subprocess_check_output(cmd, raise_exception=raise_exception)


def get_mount_point_by_dev(
    dev: str, *, raise_exception: bool = True
) -> str:  # pragma: no cover
    """Get the FIRST mountpoint of the <dev>.

    This is implemented by calling:
        findmnt <dev> -nfo TARGET <dev>

    NOTE: option -f is used to only show the first file system.

    Args:
        dev (str): the device to check against.
        raise_exception (bool, optional): raise exception on subprocess call failed.
            Defaults to True.

    Returns:
        str: the FIRST mountpint of the <dev>, or empty string if <raise_exception> is False
            and the subprocess call failed(due to dev is not mounted or other reasons).
    """
    cmd = ["findmnt", "-nfo", "TARGET", dev]
    return subprocess_check_output(cmd, raise_exception=raise_exception)


def get_dev_by_mount_point(
    mount_point: str, *, raise_exception: bool = True
) -> str:  # pragma: no cover
    """Return the source dev of the given <mount_point>.

    This is implemented by calling:
        findmnt -no SOURCE <mount_point>

    Args:
        mount_point (str): mount_point to check against.
        raise_exception (bool, optional): raise exception on subprocess call failed.
            Defaults to True.

    Returns:
        str: the source device of <mount_point>.
    """
    cmd = ["findmnt", "-no", "SOURCE", mount_point]
    return subprocess_check_output(cmd, raise_exception=raise_exception)


def is_target_mounted(
    target: StrOrPath, *, raise_exception: bool = False
) -> bool:  # pragma: no cover
    """Check if <target> is mounted or not. <target> can be a dev or a mount point.

    This is implemented by calling:
        findmnt <target>

    Args:
        target (StrOrPath): the target to check against. Could be a device or a mount point.
        raise_exception (bool, optional): raise exception on subprocess call failed.
            Defaults to True.

    Returns:
        Return True if the target has at least one mount_point. Return False if <raise_exception> is False and
            <target> is not a mount point or not mounted.
    """
    cmd = ["findmnt", target]
    try:
        subprocess_call(cmd, raise_exception=True)
        return True
    except CalledProcessError:
        if raise_exception:
            raise
        return False


def get_parent_dev(
    child_device: str, *, raise_exception: bool = True
) -> str:  # pragma: no cover
    """Get the parent devpath from <child_device>.

    When `/dev/nvme0n1p1` is specified as child_device, /dev/nvme0n1 is returned.

    This function is implemented by calling:
        lsblk -idpno PKNAME <child_device>

    Args:
        child_device (str): the device to find parent device from.
        raise_exception (bool, optional): raise exception on subprocess call failed.
            Defaults to True.

    Returns:
        str: the parent device of the specific <child_device>.
    """
    cmd = ["lsblk", "-idpno", "PKNAME", child_device]
    return subprocess_check_output(cmd, raise_exception=raise_exception)


def get_device_tree(
    parent_dev: str, *, raise_exception: bool = True
) -> list[str]:  # pragma: no cover
    """Get the device tree of a parent device.

    For example, for sda with 3 partitions, we will get:
    ["/dev/sda", "/dev/sda1", "/dev/sda2", "/dev/sda3"]

    This function is implemented by calling:
        lsblk -lnpo NAME <parent_dev>

    Args:
        parent_dev (str): The parent device to be checked.
        raise_exception (bool, optional): raise exception on subprocess call failed.
            Defaults to True.

    Returns:
        str: _description_
    """
    cmd = ["lsblk", "-lnpo", "NAME", parent_dev]
    raw_res = subprocess_check_output(cmd, raise_exception=raise_exception)
    return raw_res.splitlines()


def set_ext4_fslabel(
    dev: str, fslabel: str, *, raise_exception: bool = True
) -> None:  # pragma: no cover
    """Set <fslabel> to ext4 formatted <dev>.

    This is implemented by calling:
        e2label <dev> <fslabel>

    Args:
        dev (str): the ext4 partition device.
        fslabel (str): the fslabel to be set.
        raise_exception (bool, optional): raise exception on subprocess call failed.
            Defaults to True.
    """
    cmd = ["e2label", dev, fslabel]
    subprocess_call(cmd, raise_exception=raise_exception)


def mkfs_ext4(
    dev: str,
    *,
    fslabel: str | None = None,
    fsuuid: str | None = None,
    raise_exception: bool = True,
) -> None:  # pragma: no cover
    """Create new ext4 formatted filesystem on <dev>, optionally with <fslabel>
        and/or <fsuuid>.

    Args:
        dev (str): device to be formatted to ext4.
        fslabel (Optional[str], optional): fslabel of the new ext4 filesystem. Defaults to None.
            When it is None, this function will try to preserve the previous fslabel.
        fsuuid (Optional[str], optional): fsuuid of the new ext4 filesystem. Defaults to None.
            When it is None, this function will try to preserve the previous fsuuid.
        raise_exception (bool, optional): raise exception on subprocess call failed.
            Defaults to True.
    """
    cmd = ["mkfs.ext4", "-F"]

    if not fsuuid:
        try:
            fsuuid = get_attrs_by_dev("UUID", dev)
            assert fsuuid
            logger.debug(f"reuse previous UUID: {fsuuid}")
        except Exception:
            pass
    if fsuuid:
        logger.debug(f"using UUID: {fsuuid}")
        cmd.extend(["-U", fsuuid])

    if not fslabel:
        try:
            fslabel = get_attrs_by_dev("LABEL", dev)
            assert fslabel
            logger.debug(f"reuse previous fs LABEL: {fslabel}")
        except Exception:
            pass
    if fslabel:
        logger.debug(f"using fs LABEL: {fslabel}")
        cmd.extend(["-L", fslabel])

    cmd.append(dev)
    logger.warning(f"format {dev} to ext4: {cmd=}")
    subprocess_call(cmd, raise_exception=raise_exception)


def reboot(args: list[str] | None = None) -> NoReturn:  # pragma: no cover
    """Reboot the system, with optional args passed to reboot command.

    This is implemented by calling:
        reboot [args[0], args[1], ...]

    NOTE(20230614): this command makes otaclient exit immediately.
    NOTE(20240421): rpi_boot's reboot takes args.

    Args:
        args (Optional[list[str]], optional): args passed to reboot command.
            Defaults to None, not passing any args.

    Raises:
        CalledProcessError for the reboot call, or SystemExit on sys.exit(0).
    """
    cmd = ["reboot"]
    if args:
        logger.info(f"will reboot with argument: {args=}")
        cmd.extend(args)

    logger.warning("system will reboot now!")
    subprocess_call(cmd, raise_exception=True)
    sys.exit(0)


#
# ------ mount related helpers ------ #
#

MAX_RETRY_COUNT = 6
RETRY_INTERVAL = 2


class MountHelper(Protocol):
    """Protocol for mount helper functions.

    This is for typing purpose.
    """

    def __call__(
        self,
        target: StrOrPath,
        mount_point: StrOrPath,
        *,
        raise_exception: bool = True,
        **kwargs,
    ) -> None: ...


def mount(
    target: StrOrPath,
    mount_point: StrOrPath,
    *,
    options: list[str] | None = None,
    params: list[str] | None = None,
    raise_exception: bool = True,
) -> None:  # pragma: no cover
    """Thin wrapper to call mount using subprocess.

    This will call the following:
        mount [-o <option1>,[<option2>[,...]] [<param1> [<param2>[...]]] <target> <mount_point>

    Args:
        target (StrOrPath): The target device to mount.
        mount_point (StrOrPath): The mount point to mount to.
        options (list[str] | None, optional): A list of options, append after -o. Defaults to None.
        params (list[str] | None, optional): A list of params. Defaults to None.
        raise_exception (bool, optional): Whether to raise exception on failed call. Defaults to True.
    """
    cmd = ["mount"]
    if options:
        cmd.extend(["-o", ",".join(options)])
    if params:
        cmd.extend(params)
    cmd = [*cmd, str(target), str(mount_point)]
    subprocess_call(cmd, raise_exception=raise_exception)


def mount_rw(
    target: str, mount_point: StrOrPath, *, raise_exception: bool = True
) -> None:  # pragma: no cover
    """Mount the <target> to <mount_point> read-write.

    This is implemented by calling:
        mount -o rw --make-private --make-unbindable <target> <mount_point>

    NOTE: pass args = ["--make-private", "--make-unbindable"] to prevent
            mount events propagation to/from this mount point.

    Args:
        target (str): target to be mounted.
        mount_point (StrOrPath): mount point to mount to.
        raise_exception (bool, optional): raise exception on subprocess call failed.
            Defaults to True.
    """
    # fmt: off
    cmd = [
        "mount",
        "-o", "rw",
        "--make-private", "--make-unbindable",
        target,
        str(mount_point),
    ]
    # fmt: on
    subprocess_call(cmd, raise_exception=raise_exception)


def bind_mount_ro(
    target: str, mount_point: StrOrPath, *, raise_exception: bool = True
) -> None:  # pragma: no cover
    """Bind mount the <target> to <mount_point> read-only.

    This is implemented by calling:
        mount -o bind,ro --make-private --make-unbindable <target> <mount_point>

    Args:
        target (str): target to be mounted.
        mount_point (StrOrPath): mount point to mount to.
        raise_exception (bool, optional): raise exception on subprocess call failed.
            Defaults to True.
    """
    # fmt: off
    cmd = [
        "mount",
        "-o", "bind,ro",
        "--make-private", "--make-unbindable",
        target,
        str(mount_point)
    ]
    # fmt: on
    subprocess_call(cmd, raise_exception=raise_exception)


def mount_ro(
    target: str, mount_point: StrOrPath, *, raise_exception: bool = True
) -> None:  # pragma: no cover
    """Mount <target> to <mount_point> read-only.

    If the target device is mounted, we bind mount the target device to mount_point.
    if the target device is not mounted, we directly mount it to the mount_point.

    Args:
        target (str): target to be mounted.
        mount_point (StrOrPath): mount point to mount to.
        raise_exception (bool, optional): raise exception on subprocess call failed.
            Defaults to True.
    """
    # NOTE: set raise_exception to false to allow not mounted
    #       not mounted dev will have empty return str
    if _active_mount_point := get_mount_point_by_dev(target, raise_exception=False):
        bind_mount_ro(
            _active_mount_point,
            mount_point,
            raise_exception=raise_exception,
        )
    else:
        # target is not mounted, we mount it by ourself
        # fmt: off
        cmd = [
            "mount",
            "-o", "ro",
            "--make-private", "--make-unbindable",
            target,
            str(mount_point),
        ]
        # fmt: on
        subprocess_call(cmd, raise_exception=raise_exception)


def umount(
    target: StrOrPath, *, raise_exception: bool = True
) -> None:  # pragma: no cover
    """Try to umount the <target>.

    This is implemented by calling:
        umount <target>

    Before calling umount, the <target> will be check whether it is mounted,
        if it is not mounted, this function will return directly.

    Args:
        target (StrOrPath): target to be umounted.
        raise_exception (bool, optional): raise exception on subprocess call failed.
            Defaults to True.
    """
    # first try to check whether the target(either a mount point or a dev)
    # is mounted
    if not is_target_mounted(target, raise_exception=False):
        return

    # if the target is mounted, try to unmount it.
    _cmd = ["umount", str(target)]
    subprocess_call(_cmd, raise_exception=raise_exception)


def ensure_mount(
    target: StrOrPath,
    mnt_point: StrOrPath,
    *,
    mount_func: MountHelper,
    raise_exception: bool,
    max_retry: int = MAX_RETRY_COUNT,
    retry_interval: int = RETRY_INTERVAL,
) -> None:  # pragma: no cover
    """Ensure the <target> mounted on <mnt_point> by our best.

    Raises:
        If <raise_exception> is True, raises the last failed attemp's CalledProcessError.
    """
    for _retry in range(max_retry + 1):
        try:
            mount_func(target=target, mount_point=mnt_point)
            is_target_mounted(mnt_point, raise_exception=True)
            return
        except CalledProcessError as e:
            logger.error(
                f"retry#{_retry} failed to mount {target} on {mnt_point}: {e!r}"
            )
            logger.error(f"{e.stderr=}\n{e.stdout=}")

            if _retry >= max_retry:
                logger.error(
                    f"exceed max retry count mounting {target} on {mnt_point}, abort"
                )
                if raise_exception:
                    raise
                return

            time.sleep(retry_interval)
            continue


def ensure_umount(
    mnt_point: StrOrPath,
    *,
    ignore_error: bool,
    max_retry: int = MAX_RETRY_COUNT,
    retry_interval: int = RETRY_INTERVAL,
) -> None:  # pragma: no cover
    """Try to umount the <mnt_point> at our best.

    Raises:
        If <ignore_error> is False, raises the last failed attemp's CalledProcessError.
    """
    for _retry in range(max_retry + 1):
        try:
            if not is_target_mounted(mnt_point, raise_exception=False):
                break
            umount(mnt_point, raise_exception=True)
        except CalledProcessError as e:
            logger.warning(f"retry#{_retry} failed to umount {mnt_point}: {e!r}")
            logger.warning(f"{e.stderr}\n{e.stdout}")

            if _retry >= max_retry:
                logger.error(f"reached max retry on umounting {mnt_point}, abort")
                if not ignore_error:
                    raise
                return

            time.sleep(retry_interval)
            continue


def ensure_mointpoint(
    mnt_point: StrOrPath, *, ignore_error: bool
) -> None:  # pragma: no cover
    """Ensure the <mnt_point> exists, has no mount on it and ready for mount.

    If the <mnt_point> is valid, but we failed to umount any previous mounts on it,
        we still keep use the mountpoint as later mount will override the previous one.
    """
    mnt_point = Path(mnt_point)
    if mnt_point.is_symlink() or not mnt_point.is_dir():
        mnt_point.unlink(missing_ok=True)

    if not mnt_point.exists():
        mnt_point.mkdir(exist_ok=True, parents=True)
        return

    try:
        ensure_umount(mnt_point, ignore_error=False)
    except Exception as e:
        if not ignore_error:
            logger.error(f"failed to prepare {mnt_point=}: {e!r}")
            raise
        logger.warning(
            f"failed to prepare {mnt_point=}: {e!r} \n"
            f"But still use {mnt_point} and override the previous mount"
        )
