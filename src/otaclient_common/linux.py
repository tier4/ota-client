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
import ctypes.util
import logging
import os
import shlex
import stat
import subprocess
from pathlib import Path
from subprocess import check_call
from typing import Any, Callable, Optional

from otaclient_common._env import RUN_AS_PYINSTALLER_BUNDLE
from otaclient_common._typing import StrOrPath, copy_callable_typehint

try:
    from shutil import _fastcopy_sendfile  # type: ignore
except ImportError:
    _fastcopy_sendfile = None

try:
    from os import setns as _setns  # type: ignore
except ImportError:  # for python < 3.12
    # NOTE(20251029): actually the below code will not be used basically,
    #                   when otaclient(v3.12+) distributed as app image,
    #                   it will be distributed under python3.13.
    #
    #                 if somehow otaclient(v3.12+) is installed with venv,
    #                   we are not in app image environment, the below code
    #                   will also not be used.
    _libc_path = ctypes.util.find_library("c")
    _libc = ctypes.CDLL(_libc_path, use_errno=True)
    _libc_setns = _libc.setns

    def _setns(_fd: int, _nstype=0) -> None:
        if _libc_setns(_fd, _nstype) != 0:
            _last_e = ctypes.get_errno()
            raise OSError(f"setns failed: {os.strerror(_last_e)}")


def setns_wrapper(_fd: str | int, _nstype: int = 0) -> None:
    if isinstance(_fd, int):
        return _setns(_fd, _nstype)

    _opened_fd = os.open(_fd, os.O_RDONLY)
    try:
        _setns(_opened_fd, _nstype)
    finally:
        os.close(_opened_fd)


_root_mnt_ns = "/proc/1/ns/mnt"

logger = logging.getLogger(__name__)

#
# ------ swapfile handling ------ #
#


def create_swapfile(
    swapfile_fpath: str | Path, size_in_mibytes: int, *, timeout=900
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
            f"count={size_in_mibytes}",
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
            raise ValueError(f"invalid or missing {passwd_fpath=}: {e!r}") from None


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
            raise ValueError(f"invalid or missing {group_fpath=}: {e!r}") from None


def map_uid_by_pwnam(*, src_db: ParsedPasswd, dst_db: ParsedPasswd, uid: int) -> int:
    """Perform src_uid -> src_name -> dst_name -> dst_uid mapping.

    Raises:
        ValueError on failed mapping.
    """
    try:
        return dst_db._by_name[src_db._by_uid[uid]]
    except KeyError:
        raise ValueError(f"failed to find mapping for {uid}") from None


def map_gid_by_grpnam(*, src_db: ParsedGroup, dst_db: ParsedGroup, gid: int) -> int:
    """Perform src_gid -> src_name -> dst_name -> dst_gid mapping.

    Raises:
        ValueError on failed mapping.
    """
    try:
        return dst_db._by_name[src_db._by_gid[gid]]
    except KeyError:
        raise ValueError(f"failed to find mapping for {gid}") from None


#
# ------ subprocess call ------ #
#


@copy_callable_typehint(subprocess.run)
def pyinstaller_aware_subprocess_run(args, *p_pargs, env=None, **p_kwargs):
    """
    See https://pyinstaller.org/en/stable/runtime-information.html#ld-library-path-libpath-considerations
        for more details.
    """
    logger.debug(f"subprocess call: {args}")
    lp_key = "LD_LIBRARY_PATH"  # for GNU/Linux and *BSD.
    lp_orig_key = f"{lp_key}_ORIG"

    if env is None:
        _parsed_env = dict(os.environ)
    else:
        _parsed_env = dict(env)

    lp_orig = _parsed_env.pop(lp_orig_key, None)
    if lp_orig is not None:
        _parsed_env[lp_key] = lp_orig  # restore the original, unmodified value
    else:
        # This happens when LD_LIBRARY_PATH was not set.
        # Remove the env var as a last resort:
        _parsed_env.pop(lp_key, None)
    return subprocess.run(args, *p_pargs, env=_parsed_env, **p_kwargs)


def subprocess_run_wrapper(
    cmd: str | list[str],
    *,
    check: bool,
    check_output: bool,
    chroot: StrOrPath | None = None,
    set_host_mnt_ns: bool = False,
    env: Optional[dict[str, str]] = None,
    timeout: Optional[float] = None,
) -> subprocess.CompletedProcess[bytes]:
    """A wrapper for subprocess.run method.

    NOTE: this is for the requirement of customized subprocess call
        in the future, like chroot or nsenter before execution.

    NOTE(20250916): now subprocess_run_wrapper is pyinstaller aware.

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
    if chroot or set_host_mnt_ns:

        def _preexec():
            if chroot:
                os.chroot(chroot)
                os.chdir("/")

            # NOTE(20251029): only support sent back to host mnt ns
            if set_host_mnt_ns:
                setns_wrapper(_root_mnt_ns)

        preexec_fn = _preexec

    _run_func = subprocess.run
    if RUN_AS_PYINSTALLER_BUNDLE:
        _run_func = pyinstaller_aware_subprocess_run

    return _run_func(
        cmd,
        check=check,
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE if check_output else None,
        timeout=timeout,
        preexec_fn=preexec_fn,
        env=env,
    )  # type: ignore


#
# ------ optimized file IO on linux ------ #
#


def _copyfileobj(fsrc, fdst, length=8 * 1024**2):
    """Copied from shutil.copyfileobj."""
    fsrc_read = fsrc.read
    fdst_write = fdst.write
    while buf := fsrc_read(length):
        fdst_write(buf)


def copyfile_nocache(src: StrOrPath, dst: StrOrPath) -> None:
    """Much simpler version of shutil.copyfile, but with configuring fadvise.

    As our use case is simpler, this function skips many checks that
        shutil.copyfile will do.
    """
    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
        src_fd = fsrc.fileno()
        dst_fd = fdst.fileno()
        try:
            os.posix_fadvise(src_fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)
            try:
                if _fastcopy_sendfile:
                    _fastcopy_sendfile(fsrc, fdst)
                    return
            except OSError:
                raise
            except Exception:
                # exceptions raised by _fastcopy_sendfile when it finds that
                #   sendfile syscall is not supported.
                pass

            _copyfileobj(fsrc, fdst)
        finally:
            os.posix_fadvise(src_fd, 0, 0, os.POSIX_FADV_DONTNEED)
            os.posix_fadvise(dst_fd, 0, 0, os.POSIX_FADV_DONTNEED)


#
# ------ fstrim with timeout ------ #
#


def fstrim_at_subprocess(
    target_mountpoint: Path, *, wait: bool = True, timeout: int
) -> None:  # pragma: no cover
    """Dispatch subprocess to do fstrim with timeout.

    fstrim is safe to be interrupted at any time.

    Args:
        target_mountpoint: target to do fstrim against.
        wait: if True, will wait for fstrim finish up(blocking),
            otherwise, spawn a background detached process to do fstrim.
        timeout: max execution time for fstrim.
    """
    _cmd = ["fstrim", str(target_mountpoint)]
    _run_func = subprocess.run
    if RUN_AS_PYINSTALLER_BUNDLE:
        _run_func = pyinstaller_aware_subprocess_run

    if wait:  # spawned the fstrim subprocess, and wait for it finishes.
        try:
            _run_func(
                _cmd,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )  # type: ignore
        except Exception:
            pass
        return

    # the spawned subprocess will do a double-fork to fully detach
    #   the fstrim command execution from otaclient process.
    _cmd = ["nohup", "timeout", str(timeout), *_cmd]
    _detached_cmd = f"{shlex.join(_cmd)} &"
    _run_func(
        _detached_cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=True,
    )  # type: ignore


def is_non_empty_regular_file(path: Path) -> bool:
    """Check if a path is non empty regular."""
    try:
        path_stat = path.lstat()
        return (
            stat.S_ISREG(path_stat.st_mode)
            and not stat.S_ISLNK(path_stat.st_mode)
            and path_stat.st_size > 0
        )
    except OSError:
        return False


def is_file_or_symlink(path: Path) -> bool:
    """Check if a path is regular or symlink."""
    try:
        path_stat = path.lstat()
        return stat.S_ISREG(path_stat.st_mode) or stat.S_ISLNK(path_stat.st_mode)
    except OSError:
        return False


def is_directory(path: Path) -> bool:
    """Check if a path is a real directory."""
    try:
        path_stat = path.lstat()
        return stat.S_ISDIR(path_stat.st_mode) and not stat.S_ISLNK(path_stat.st_mode)
    except OSError:
        return False
