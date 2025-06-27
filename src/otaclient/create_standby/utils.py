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

import logging
import os
from pathlib import Path
from typing import Generator

from otaclient_common import human_readable_size
from otaclient_common._typing import StrOrPath
from otaclient_common.cmdhelper import (
    ensure_mount,
    ensure_umount,
    get_attrs_by_dev,
    mount_ro,
)
from otaclient_common.linux import subprocess_run_wrapper

logger = logging.getLogger(__name__)


class TopDownCommonShortestPath:
    """Assume that the disk scan is top-down style."""

    def __init__(self) -> None:
        self._store: set[Path] = set()

    def add_path(self, _path: Path):
        _path = Path(_path).resolve()
        for _parent in _path.parents:
            # this path is covered by a shorter common prefix
            if _parent in self._store:
                return
        self._store.add(_path)

    def iter_paths(self) -> Generator[Path]:
        yield from self._store


def _check_if_ext4(dev: StrOrPath) -> bool:  # pragma: no cover
    return get_attrs_by_dev("FSTYPE", dev) == "ext4"


def _check_if_fs_healthy(dev: StrOrPath) -> bool:  # pragma: no cover
    _res = subprocess_run_wrapper(
        ["e2fsck", "-n", str(dev)],
        check=False,
        check_output=False,
    )
    return _res.returncode == 0


def _get_fs_used_size(mnt_point: StrOrPath) -> int:  # pragma: no cover
    stats = os.statvfs(mnt_point)
    used_in_bytes = (stats.f_blocks - stats.f_bfree) * stats.f_frsize
    logger.info(f"fs on {mnt_point=} used size: {human_readable_size(used_in_bytes)}")
    return used_in_bytes


def _check_fs_used_size_reach_threshold(
    dev: StrOrPath, mnt_point: StrOrPath, threshold_in_bytes: int
) -> bool:  # pragma: no cover
    try:
        ensure_mount(dev, mnt_point, mount_func=mount_ro, raise_exception=True)
        return _get_fs_used_size(mnt_point) >= threshold_in_bytes
    except Exception as e:
        logger.warning(f"failed to mount standby slot ({dev=}: {e!r}")
        return False
    finally:
        ensure_umount(mnt_point, ignore_error=True)


def can_use_in_place_mode(
    dev: StrOrPath, mnt_point: StrOrPath, threshold_in_bytes: int
) -> bool:  # pragma: no cover
    """
    Check whether target standby slot device is ready for in-place update mode.

    The following checks will be performed in order:
    1. standby slot contains ext4 partition.
    2. standby slot's fs is healthy.
    3. standby slot's used size reaches threshold.
    """
    if not (_check_if_ext4(dev) and _check_if_fs_healthy(dev)):
        return False
    return _check_fs_used_size_reach_threshold(dev, mnt_point, threshold_in_bytes)
