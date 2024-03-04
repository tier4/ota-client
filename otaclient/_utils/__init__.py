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
from math import ceil
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar
from typing_extensions import Literal, ParamSpec, Concatenate

P = ParamSpec("P")


def copy_callable_typehint(_source: Callable[P, Any]):
    """This helper function return a decorator that can type hint the target
    function as the _source function.

    At runtime, this decorator actually does nothing, but just return the input function as it.
    But the returned function will have the same type hint as the source function in ide.
    It will not impact the runtime behavior of the decorated function.
    """

    def _decorator(target) -> Callable[P, Any]:
        return target

    return _decorator


RT = TypeVar("RT")


def copy_callable_typehint_to_method(_source: Callable[P, Any]):
    """Works the same as copy_callable_typehint, but omit the first arg."""

    def _decorator(target: Callable[..., RT]) -> Callable[Concatenate[Any, P], RT]:
        return target  # type: ignore

    return _decorator


_MultiUnits = Literal["GiB", "MiB", "KiB", "Bytes", "KB", "MB", "GB"]
# fmt: off
_multiplier: dict[_MultiUnits, int] = {
    "GiB": 1024 ** 3, "MiB": 1024 ** 2, "KiB": 1024 ** 1,
    "GB": 1000 ** 3, "MB": 1000 ** 2, "KB": 1000 ** 1,
    "Bytes": 1,
}
# fmt: on


def get_file_size(
    swapfile_fpath: str | Path, units: _MultiUnits = "Bytes"
) -> Optional[int]:
    """Helper for get file size with <units>."""
    swapfile_fpath = Path(swapfile_fpath)
    if swapfile_fpath.is_file():
        return ceil(swapfile_fpath.stat().st_size / _multiplier[units])
