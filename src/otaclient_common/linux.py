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


from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from subprocess import check_call
from typing import Any, Callable, Optional

from otaclient_common.typing import StrOrPath

#
# ------ swapfile handling ------ #
#


def create_swapfile(
    swapfile_fpath: str | Path, size_in_MiB: int, *, timeout=900
) -> Path:
    """Create swapfile at <swapfile_fpath> with <size_in_MiB>MiB.

    Reference: https://wiki.archlinux.org/title/swap#Swap_file_creation

    Args:
        swapfile_fpath(StrOrPath): the path to place the created swapfile.
        size_in_MiB(int): the size of to-be-created swapfile.
        timeout: timeout of swapfile creating, default is 15mins.

    Returns:
        The Path object to the newly created swapfile.

    Raises:
        ValueError on file already exists at <swapfile_fpath>, SubprocessCallFailed
            on failed swapfile creation.
    """
    swapfile_fpath = Path(swapfile_fpath)
    if swapfile_fpath.exists():
        raise ValueError(f"{swapfile_fpath=} exists, skip")

    # create a new file with <size_in_MiB>MiB size
    # executes:
    #   dd if=/dev/zero of=/swapfile bs=1M count=8k
    #   chmod 0600 /swapfile
    check_call(
        [
            "dd",
            "if=/dev/zero",
            f"of={str(swapfile_fpath)}",
            "bs=1M",
            f"count={size_in_MiB}",
        ],
        timeout=timeout,
    )
    swapfile_fpath.chmod(0o600)

    # prepare the created file as swapfile
    # executes:
    #   mkswap /swapfile
    check_call(["mkswap", str(swapfile_fpath)], timeout=timeout)

    return swapfile_fpath


#
# ------ gid/uid mapping ------ #
#

_SPLITTER = ":"


class ParsedPasswd:
    """Parse passwd and store name/uid mapping.

    Example passwd entry line:
    nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin

    Attrs:
        _by_name (dict[str, int]): name:uid mapping.
        _by_uid (dict[int, str]): uid:name mapping.
    """

    __slots__ = ["_by_name", "_by_uid"]

    def __init__(self, passwd_fpath: str | Path) -> None:
        self._by_name: dict[str, int] = {}
        try:
            with open(passwd_fpath, "r") as f:
                for line in f:
                    _raw_list = line.strip().split(_SPLITTER)
                    _name, _uid = _raw_list[0], int(_raw_list[2])
                    self._by_name[_name] = _uid
            self._by_uid = {v: k for k, v in self._by_name.items()}
        except Exception as e:
            raise ValueError(f"invalid or missing {passwd_fpath=}: {e!r}")


class ParsedGroup:
    """Parse group and store name/gid mapping.

    Example group entry line:
    nogroup:x:65534:

    Attrs:
        _by_name (dict[str, int]): name:gid mapping.
        _by_gid (dict[int, str]): gid:name mapping.
    """

    __slots__ = ["_by_name", "_by_gid"]

    def __init__(self, group_fpath: str | Path) -> None:
        self._by_name: dict[str, int] = {}
        try:
            with open(group_fpath, "r") as f:
                for line in f:
                    _raw_list = line.strip().split(_SPLITTER)
                    self._by_name[_raw_list[0]] = int(_raw_list[2])
            self._by_gid = {v: k for k, v in self._by_name.items()}
        except Exception as e:
            raise ValueError(f"invalid or missing {group_fpath=}: {e!r}")


def map_uid_by_pwnam(*, src_db: ParsedPasswd, dst_db: ParsedPasswd, uid: int) -> int:
    """Perform src_uid -> src_name -> dst_name -> dst_uid mapping.

    Raises:
        ValueError on failed mapping.
    """
    try:
        return dst_db._by_name[src_db._by_uid[uid]]
    except KeyError:
        raise ValueError(f"failed to find mapping for {uid}")


def map_gid_by_grpnam(*, src_db: ParsedGroup, dst_db: ParsedGroup, gid: int) -> int:
    """Perform src_gid -> src_name -> dst_name -> dst_gid mapping.

    Raises:
        ValueError on failed mapping.
    """
    try:
        return dst_db._by_name[src_db._by_gid[gid]]
    except KeyError:
        raise ValueError(f"failed to find mapping for {gid}")


#
# ------ subprocess call ------ #
#


def subprocess_run_wrapper(
    cmd: str | list[str],
    *,
    check: bool,
    check_output: bool,
    chroot: Optional[StrOrPath] = None,
    env: Optional[dict[str, str]] = None,
    timeout: Optional[float] = None,
) -> subprocess.CompletedProcess[bytes]:
    """A wrapper for subprocess.run method.

    NOTE: this is for the requirement of customized subprocess call
        in the future, like chroot or nsenter before execution.

    Args:
        cmd (str | list[str]): command to be executed.
        check (bool): if True, raise CalledProcessError on non 0 return code.
        check_output (bool): if True, the UTF-8 decoded stdout will be returned.
        timeout (Optional[float], optional): timeout for execution. Defaults to None.

    Returns:
        subprocess.CompletedProcess[bytes]: the result of the execution.
    """
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)

    preexec_fn: Optional[Callable[..., Any]] = None
    if chroot:

        def _chroot():
            os.chroot(chroot)

        preexec_fn = _chroot

    return subprocess.run(
        cmd,
        check=check,
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE if check_output else None,
        timeout=timeout,
        preexec_fn=preexec_fn,
        env=env,
    )
