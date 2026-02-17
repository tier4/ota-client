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

import os
from math import ceil
from pathlib import Path
from typing import Literal

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
) -> int | None:
    """Helper for get file size with <units>."""
    swapfile_fpath = Path(swapfile_fpath)
    if swapfile_fpath.is_file():
        return ceil(swapfile_fpath.stat().st_size / _multiplier[units])


def human_readable_size(_in: int) -> str:
    for _mu_name, _mu in _multiplier.items():
        if _mu == 1:
            break
        if (_res := (_in / _mu)) > 1:
            return f"{_res:.2f} {_mu_name}"
    return f"{_in} Bytes"


def replace_root(
    path: str | Path, old_root: str | Path | Literal["/"], new_root: str | Path
) -> str:
    """Replace a <path> relative to <old_root> to <new_root>.

    For example, if path="/abc", old_root="/", new_root="/new_root",
    then we will have "/new_root/abc".
    """
    old_root, new_root = str(old_root), str(new_root)
    if not (old_root.startswith("/") and new_root.startswith("/")):
        raise ValueError(f"{old_root=} and/or {new_root=} is not valid root")
    if os.path.commonpath([path, old_root]) != old_root:
        raise ValueError(f"{old_root=} is not the root of {path=}")
    return os.path.join(new_root, os.path.relpath(path, old_root))


EMPTY_FILE_SHA256 = r"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
EMPTY_FILE_SHA256_BYTE = bytes.fromhex(
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)
SHA256DIGEST_HEX_LEN = len(EMPTY_FILE_SHA256)
SHA256DIGEST_LEN = len(EMPTY_FILE_SHA256_BYTE)
