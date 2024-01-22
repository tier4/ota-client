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
import subprocess
import shlex
from subprocess import CalledProcessError, TimeoutExpired, SubprocessError
from typing import TYPE_CHECKING, Callable, Optional
from typing_extensions import TypeAlias

from . import truncate_str
from .typing import ArgsType
from .linux import INIT_PID, NS_NAME_LITERAL, nsenter

# prevent too-long stdout/stderr in err when handling exception
_ERR_MAX_LEN = 2048

# avoid manually import from std subprocess module when using this module
SubProcessCallFailed: TypeAlias = CalledProcessError
SubProcessCallTimeoutExpired: TypeAlias = TimeoutExpired


def gen_err_report(_in: SubProcessCallFailed | SubProcessCallTimeoutExpired) -> str:
    """Compose error report from exception."""
    if isinstance(_in, SubProcessCallTimeoutExpired):
        return f"{_in!r}"
    return (
        f"{_in!r}\n"
        f"return_code=({_in.returncode}, \n"
        f"stderr={truncate_str(_in.stderr.decode(), _ERR_MAX_LEN)}, \n"
        f"stdout={truncate_str(_in.stdout.decode(), _ERR_MAX_LEN)})\n"
    )


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

    except Exception:
        if raise_exception:
            raise


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
            SubprocessCalledFailed exception if subprocess call failed, SubProcessCalledTimeoutExpired if cmd execution
                timeout is defined and reached.
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
            SubprocessCalledFailed exception if subprocess call failed, SubProcessCalledTimeoutExpired if cmd execution
                timeout is defined and reached.
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
