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
from typing import TYPE_CHECKING, Callable, Optional

from . import truncate_str_or_bytes

# prevent too-long stdout/stderr in err when handling exception
_ERR_MAX_LEN = 2048


class SubProcessCalledFailed(Exception):
    def __init__(
        self,
        cmd: str = "",
        *,
        return_code: int = -1,
        stdout: str = "",
        stderr: str = "",
        new_root: Optional[str] = None,
        msg: str = "",
    ) -> None:
        self.cmd = cmd
        self.return_code = return_code
        self.stdout = truncate_str_or_bytes(stdout, _ERR_MAX_LEN)
        self.stderr = truncate_str_or_bytes(stderr, _ERR_MAX_LEN)
        self.new_root = new_root
        self.msg = msg
        super().__init__(f"{cmd=} execution failed({new_root=}): {msg}")

    @property
    def err_report(self):
        if self.cmd:
            return (
                f"cmd({self.cmd}) execution failed(new_root={self.new_root}): {self.msg}\n"
                f"return_code=({self.return_code}, \n"
                f"stderr={truncate_str_or_bytes(self.stderr, _ERR_MAX_LEN)}, \n"
                f"stdout={truncate_str_or_bytes(self.stdout, _ERR_MAX_LEN)})\n"
            )
        return f"other failure: {self.msg}"


def _subprocess_call(
    cmd: str | list[str],
    *,
    new_root: Optional[str] = None,
    raise_exception: bool = False,
    timeout: Optional[float] = None,
    capture_output: bool = False,
    default: Optional[str] = None,
) -> str | None:
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)

    _preexec_fn: Optional[Callable[[], None]] = None
    if new_root:
        if not os.path.isdir(new_root):
            raise SubProcessCalledFailed(
                msg=f"try to run cmd={' '.join(cmd)} with chroot, but {new_root=} is not a dir"
            )
        _preexec_fn = functools.partial(os.chroot, new_root)

    try:
        _res = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            preexec_fn=_preexec_fn,
            timeout=timeout,
        )
        if capture_output:
            return _res.stdout.decode()

    except subprocess.CalledProcessError as e:
        if raise_exception:
            raise SubProcessCalledFailed(
                " ".join(cmd),
                return_code=e.returncode,
                new_root=new_root,
                stderr=str(e.stderr),
            ) from e
        if capture_output:
            return default

    except Exception as e:
        if raise_exception:
            raise SubProcessCalledFailed(
                msg=f"unexpected exception:{cmd=}, {e!r}"
            ) from e
        if capture_output:
            return default


if TYPE_CHECKING:

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
            new_root (str = None): if this command is required to run with chroot, specifying
                which root to chroot to.
            raise_exception (bool = False): if true, exception before/during subprocess execution
                will be raised, otherwise exception will be handled.
                Note that exception raised due to <new_root> is invalid will always be raised.
            timeout (floats = None): subprocess execution timeout.

        Raises:
            SubprocessCalledFailed exception if subprocess call failed or <new_root> is specified
                but invalid(not found or not a dir).
        """

    def subprocess_check_output(
        cmd: str | list[str],
        *,
        new_root: Optional[str] = None,
        raise_exception: bool = False,
        timeout: Optional[float] = None,
        default: Optional[str] = None,
    ) -> str | None:
        """Run <cmd> in subprocess and return the result.

        Args:
            cmd (str | list[str]): the <cmd> to be execute.
            new_root (str = None): if this command is required to run with chroot, specifying
                which root to chroot to.
            raise_exception (bool = False): if true, exception before/during subprocess execution
                will be raised, otherwise exception will be handled.
                Note that exception raised due to <new_root> is invalid will always be raised.
            timeout (floats = None): subprocess execution timeout.
            default (str = None): if subprocess execution failed but <raise_exception> is False,
                use <default> as return value.

        Returns:
            The stdout of the execution, or <default> if execution failed and <raise_exception> is False.

        Raises:
            SubprocessCalledFailed exception if subprocess call failed or <new_root> is specified
                but invalid(not found or not a dir).
        """

else:
    subprocess_call = functools.partial(_subprocess_call, capture_output=False)
    subprocess_check_output = functools.partial(_subprocess_call, capture_output=True)
