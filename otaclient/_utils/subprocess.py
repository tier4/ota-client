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
import functools
import os
import os.path
import subprocess
import shlex
from typing import Callable, Optional

from . import truncate_str_or_bytes


class SubProcessCalledFailed(Exception):
    ...


# prevent too-long stdout/stderr in err when handling exception
_ERR_MAX_LEN = 2048


def subprocess_call(
    cmd: str | list[str],
    *,
    new_root: Optional[str] = None,
    raise_exception: bool = False,
    timeout: Optional[float] = None,
) -> None:
    """Run <cmd> in subprocess without returnning the output.

    Args:
        cmd (str | list[str]): the <cmd> to be execute.
        new_root (str): if this command is required to run with chroot, specifying
            which root to chroot to.
        raise_exception (bool): if true, exception before/during subprocess execution
            will be raised, otherwise exception will be handled.
            Note that exception raised due to <new_root> is invalid will always be raised.
        timeout (floats): subprocess execution timeout.
        default (str): if subprocess execution failed but <raise_exception> is False,
            use <default> as return value.

    Raises:
        SubprocessCalledFailed exception if subprocess call failed or <new_root> is specified
            but invalid(not found or not a dir).
    """

    # NOTE: we need to check the stderr and stdout when error occurs,
    # so use subprocess.run here instead of subprocess.check_call
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)

    _preexec_fn: Optional[Callable[[], None]] = None
    if new_root:
        if not os.path.isdir(new_root):
            raise SubProcessCalledFailed(
                f"try to run {cmd} with chroot, but {new_root} is not a dir"
            )
        _preexec_fn = functools.partial(os.chroot, new_root)

    try:
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            preexec_fn=_preexec_fn,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as e:
        msg = (
            f"{cmd=} failed: \n"
            f"return_code=({e.returncode}, \n"
            f"stderr={truncate_str_or_bytes(e.stderr, _ERR_MAX_LEN)}, \n"
            f"stdout={truncate_str_or_bytes(e.stdout, _ERR_MAX_LEN)})\n"
        )
        if raise_exception:
            raise SubProcessCalledFailed(msg) from e
    except Exception as e:
        if raise_exception:
            raise SubProcessCalledFailed(f"unexpected exception: {e!r}") from e


def subprocess_check_output(
    cmd: str | list[str],
    *,
    new_root: Optional[str] = None,
    raise_exception: bool = False,
    timeout: Optional[float] = None,
    default: Optional[str] = None,
) -> str | None:
    """Run <cmd> in subprocess, return the execution result.

    Args:
        cmd (str | list[str]): the <cmd> to be execute.
        new_root (str): if this command is required to run with chroot, specifying
            which root to chroot to.
        raise_exception (bool): if true, exception before/during subprocess execution
            will be raised, otherwise exception will be handled.
            Note that exception raised due to new_root is invalid will always be raised.
        timeout (floats): subprocess execution timeout.
        default (str): if subprocess execution failed but <raise_exception> is False,
            use <default> as return value.

    Return:
        The UTF-8 decoded result from subprocess stdout, or <default> if execution failed
            but <raise_exception> is False.

    Raises:
        SubprocessCalledFailed exception if subprocess call failed or <new_root> is specified
            but invalid(not found or not a dir).
    """
    # NOTE: we need to check the stderr and stdout when error occurs,
    # so use subprocess.run here instead of subprocess.check_call
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)

    _preexec_fn: Optional[Callable[[], None]] = None
    if new_root:
        if not os.path.isdir(new_root):
            raise SubProcessCalledFailed(
                f"try to run {cmd} with chroot, but {new_root} is not a dir"
            )
        _preexec_fn = functools.partial(os.chroot, new_root)

    try:
        return subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            preexec_fn=_preexec_fn,
            timeout=timeout,
        ).stdout.decode()
    except subprocess.CalledProcessError as e:
        msg = (
            f"{cmd=} failed: \n"
            f"return_code=({e.returncode}, \n"
            f"stderr={truncate_str_or_bytes(e.stderr, _ERR_MAX_LEN)}, \n"
            f"stdout={truncate_str_or_bytes(e.stdout, _ERR_MAX_LEN)})\n"
        )
        if raise_exception:
            raise SubProcessCalledFailed(msg) from e
        return default
    except Exception as e:
        if raise_exception:
            raise SubProcessCalledFailed(f"unexpected exception: {e!r}") from e
        return default
