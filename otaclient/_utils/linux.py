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
import ctypes
import os
from contextlib import contextmanager
from errno import errorcode
from pathlib import Path
from typing import Literal

from .typing import StrOrPath


#
# ------ passwd/group database parse helper ------ #
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

    def __init__(self, passwd_fpath: StrOrPath) -> None:
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

    def __init__(self, group_fpath: StrOrPath) -> None:
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
# ------ linux namespace manipulating ------ #
#

PROC = "/proc"
NS_NAMES = ("cgroup", "ipc", "mnt", "net", "pid", "time", "user", "uts")
NS_NAME_LITERAL = Literal["cgroup", "ipc", "mnt", "net", "pid", "time", "user", "uts"]


@contextmanager
def open_target_fd(pid: int, target: str):
    _target_loc = Path(PROC) / str(pid) / target
    _fd = os.open(_target_loc, os.O_RDONLY)
    try:
        yield _fd
    finally:
        os.close(_fd)


def open_target_ns(pid: int, nsname: NS_NAME_LITERAL):
    _ns_loc = f"ns/{nsname}"
    return open_target_fd(pid, _ns_loc)


LIBC = "libc.so.6"
libc = ctypes.CDLL(LIBC)


def setns(fd: int, nstype: int = 0):
    _return_code = libc.setns(ctypes.c_int(fd), ctypes.c_int(nstype))
    if _return_code != 0:
        _err_name = errorcode[_return_code]
        raise OSError(f"setns failed with return code {_err_name}")


INIT_PID = 1


def nsenter(pid: int, *_ns_names: NS_NAME_LITERAL, chroot: bool = True) -> None:
    """Make current process enter target <pid>'s <ns_name(s)>.

    NOTE: this implementation refers to utils-linux's nsenter implementation.

    Args:
        pid(int): target process's id.
        ns_type(*NS_LITERAL_TYPE): which ns(s) we will enter.
        chroot(bool=True): chroot to target process's root after nsenter.

    Raises:
        ValueError on invalid input ns, OSError on failed sys calls.
    """
    # check input ns_type
    for _ns_name in _ns_names:
        if _ns_name not in NS_NAMES:
            raise ValueError(f"unexpected input {_ns_name=}")
    ns_names = set(_ns_names)

    # don't enter user namespace first, as we might be deprivileging ourselves
    do_user_ns = "user" in ns_names
    ns_names.discard("user")
    for _ns_name in ns_names:
        with open_target_ns(pid, _ns_name) as _ns_fd:
            setns(_ns_fd)

    # after finishing the priviledged operation, do user_ns enter
    if do_user_ns:
        with open_target_ns(pid, "user") as _ns_fd:
            setns(_ns_fd)

    if chroot:
        with open_target_fd(pid, "root") as root_fd:
            if root_fd < 0:
                raise OSError("failed to open /proc/root")

            try:
                os.fchdir(root_fd)
                os.chroot(".")
                os.chdir("/")
            except OSError as e:
                raise OSError(f"failed to chroot to {pid=}'s root") from e
