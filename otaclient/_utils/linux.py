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
from subprocess import check_output, check_call
from typing import Optional


def create_swapfile(
    swapfile_fpath: str | Path, size_in_MiB: int, *, timeout=600
) -> Path:
    """Create swapfile at <swapfile_fpath> with <size_in_MiB>MiB.

    Reference: https://wiki.archlinux.org/title/swap#Swap_file_creation

    Args:
        swapfile_fpath(StrOrPath): the path to place the created swapfile.
        size_in_MiB(int): the size of to-be-created swapfile.
        timeout: timeout of swapfile creating, default is 600 seconds.

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
            f"count={size_in_MiB}",
        ],
        timeout=timeout,
    )
    swapfile_fpath.chmod(0o600)

    # prepare the created file as swapfile
    # executes:
    #   mkswap /swapfile
    check_call(["mkswap", str(swapfile_fpath)], timeout=timeout)

    return swapfile_fpath


def detect_swapfile_size(swapfile_fpath: str | Path, *, timeout=120) -> Optional[int]:
    """Get the size of <swapfile_fpath> in MiB."""
    swapfile_fpath = Path(swapfile_fpath)
    if not swapfile_fpath.is_file():
        return

    _cmd = [
        "swapon",
        "--show=SIZE",
        "--noheadings",
        "--raw",
        "--bytes",
        str(swapfile_fpath),
    ]
    _raw_size = check_output(_cmd, timeout=timeout).decode().strip()
    return ceil(int(_raw_size) / 1024**2)
