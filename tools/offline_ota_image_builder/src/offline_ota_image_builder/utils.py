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
"""Shared utils among package."""

import os
import shlex
import subprocess
import sys
from os import PathLike
from typing import Optional, TypeAlias, Union

StrPath: TypeAlias = Union[str, PathLike]


class InputImageProcessError(Exception): ...


class ExportError(Exception): ...


RUN_AS_PYINSTALLER_BUNDLE = getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def pyinstaller_aware_subprocess_run(args, *p_pargs, env=None, **p_kwargs):
    """
    See https://pyinstaller.org/en/stable/runtime-information.html#ld-library-path-libpath-considerations
        for more details.
    """
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
    check: bool = True,
    check_output: bool = True,
    env: Optional[dict[str, str]] = None,
    timeout: Optional[float] = None,
) -> subprocess.CompletedProcess[bytes]:
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)

    _run_func = subprocess.run
    if RUN_AS_PYINSTALLER_BUNDLE:
        _run_func = pyinstaller_aware_subprocess_run

    return _run_func(
        cmd,
        check=check,
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE if check_output else None,
        timeout=timeout,
        env=env,
    )  # type: ignore
