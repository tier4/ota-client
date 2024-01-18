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
from .typing import ArgsType
from .linux import INIT_PID, NS_NAME_LITERAL, nsenter

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
        enter_root_ns: Optional[tuple[NS_NAME_LITERAL, ...]] = None,
        msg: str = "",
    ) -> None:
        self.cmd = cmd
        self.return_code = return_code
        self.stdout = truncate_str_or_bytes(stdout, _ERR_MAX_LEN)
        self.stderr = truncate_str_or_bytes(stderr, _ERR_MAX_LEN)
        self.enter_root_mount_ns = enter_root_ns
        self.msg = msg
        super().__init__(f"{cmd=} execution failed({enter_root_ns=}): {msg}")

    @property
    def err_report(self):
        if self.cmd:
            return (
                f"{self:!r}\n"
                f"return_code=({self.return_code}, \n"
                f"stderr={truncate_str_or_bytes(self.stderr, _ERR_MAX_LEN)}, \n"
                f"stdout={truncate_str_or_bytes(self.stdout, _ERR_MAX_LEN)})\n"
            )
        return f"other failure: {self.msg}"


def _subprocess_call(
    _cmd: ArgsType,
    *,
    enter_root_ns: Optional[tuple[NS_NAME_LITERAL, ...]] = None,
    raise_exception: bool = False,
    timeout: Optional[float] = None,
    capture_output: bool = False,
    strip_result: bool = True,
) -> str | None:
    cmd = shlex.split(_cmd) if isinstance(_cmd, str) else _cmd

    _preexec_fn: Optional[Callable[[], None]] = None
    if enter_root_ns:
        _preexec_fn = functools.partial(nsenter, INIT_PID, *enter_root_ns)

    try:
        _res = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            preexec_fn=_preexec_fn,
            timeout=timeout,
        )
        if capture_output:
            _res = _res.stdout.decode()
            if strip_result:
                return _res.strip()
            return _res

    except subprocess.CalledProcessError as e:
        if raise_exception:
            raise SubProcessCalledFailed(
                " ".join(cmd),
                return_code=e.returncode,
                enter_root_ns=enter_root_ns,
                stderr=str(e.stderr),
            ) from e

    except Exception as e:
        if raise_exception:
            raise SubProcessCalledFailed(
                msg=f"unexpected exception:{cmd=}, {e!r}"
            ) from e


if TYPE_CHECKING:

    def subprocess_call(
        cmd: str | list[str],
        *,
        enter_root_ns: Optional[tuple[NS_NAME_LITERAL, ...]] = None,
        raise_exception: bool = False,
        timeout: Optional[float] = None,
    ) -> None:
        """Run <cmd> in subprocess without returnning the output.

        Args:
            cmd (str | list[str]): the <cmd> to be execute.
            enter_root_ns (tuple[NS_NAME_LITERAL, ...] = None): whether to execute the command under pid 1's ns,
                default is not entering any root namespace.
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
        enter_root_ns: Optional[tuple[NS_NAME_LITERAL, ...]] = None,
        raise_exception: bool = False,
        timeout: Optional[float] = None,
        strip_result: bool = True,
    ) -> str | None:
        """Run <cmd> in subprocess and return the result.

        Args:
            cmd (str | list[str]): the <cmd> to be execute.
            enter_root_ns (tuple[NS_NAME_LITERAL, ...] = None): whether to execute the command under pid 1's ns,
                default is not entering any root namespace.
            raise_exception (bool = False): if true, exception before/during subprocess execution
                will be raised, otherwise exception will be handled.
                Note that exception raised due to <new_root> is invalid will always be raised.
            timeout (floats = None): subprocess execution timeout.
            strip_result (bool = True): whether apply strip to the execution output.

        Returns:
            The stdout of the execution, or None if execution failed and <raise_exception> is False.

        Raises:
            SubprocessCalledFailed exception if subprocess call failed or <new_root> is specified
                but invalid(not found or not a dir).
        """

else:
    subprocess_call = functools.partial(_subprocess_call, capture_output=False)
    subprocess_check_output = functools.partial(_subprocess_call, capture_output=True)


def compose_cmd(_cmd: str, _args: ArgsType) -> list[str]:
    """Compose split cmd str from combining <_cmd> with <_args>.

    For example,
        1. compose_cmd_str("echo", ["-n", "abc"]) will get ["echo", "-n", "abc"].
        2. compose_cmd_str("echo", "-n abc") will get the same ["echo", "-n", "abc"].
    """
    _parsed_args = shlex.split(_args) if isinstance(_args, str) else _args
    return [_cmd, *_parsed_args]
