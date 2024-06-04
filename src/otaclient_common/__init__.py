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
"""Common shared libs for otaclient."""


from __future__ import annotations

import importlib.util
import os
import sys
from math import ceil
from pathlib import Path
from types import ModuleType
from typing import Optional

from typing_extensions import Literal

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


def replace_root(path: str | Path, old_root: str | Path, new_root: str | Path) -> str:
    """Replace a <path> relative to <old_root> to <new_root>.

    For example, if path="/abc", old_root="/", new_root="/new_root",
    then we will have "/new_root/abc".
    """
    # normalize all the input args
    path = os.path.normpath(path)
    old_root = os.path.normpath(old_root)
    new_root = os.path.normpath(new_root)

    if not (old_root.startswith("/") and new_root.startswith("/")):
        raise ValueError(f"{old_root=} and/or {new_root=} is not valid root")
    if os.path.commonpath([path, old_root]) != old_root:
        raise ValueError(f"{old_root=} is not the root of {path=}")
    return os.path.join(new_root, os.path.relpath(path, old_root))


def import_from_file(path: Path) -> tuple[str, ModuleType]:
    if not path.is_file():
        raise ValueError(f"{path} is not a valid module file")
    try:
        _module_name = path.stem
        _spec = importlib.util.spec_from_file_location(_module_name, path)
        _module = importlib.util.module_from_spec(_spec)  # type: ignore
        _spec.loader.exec_module(_module)  # type: ignore
        return _module_name, _module
    except Exception:
        raise ImportError(f"failed to import module from {path=}.")
