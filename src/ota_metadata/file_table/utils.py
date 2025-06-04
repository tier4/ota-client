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
# TODO:(20250604): integrate.

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from typing import Literal, Optional, TypedDict

from otaclient_common._logging import get_burst_suppressed_logger
from otaclient_common._typing import StrOrPath

burst_suppressed_logger = get_burst_suppressed_logger(f"{__name__}.file_op_failed")

CANONICAL_ROOT = "/"

#
# ------ type hint helpers ------ #
#


class FileTableEntryTypedDict(TypedDict):
    """The result of joining ft_inode and ft_* table."""

    path: str
    uid: int
    gid: int
    mode: int
    links_count: Optional[int]
    xattrs: Optional[bytes]


class RegularFileTypedDict(FileTableEntryTypedDict):
    digest: bytes
    size: int
    inode_id: int


class NonRegularFileTypedDict(FileTableEntryTypedDict):
    meta: Optional[bytes]


class DirTypedDict(TypedDict):
    path: str
    uid: int
    gid: int
    mode: int


#
# ------ helper methods ------ #
#


def fpath_on_target(_canonical_path: StrOrPath, target_mnt: StrOrPath) -> Path:
    """Return the fpath of self joined to <target_mnt>."""
    _canonical_path = Path(_canonical_path)
    _target_on_mnt = Path(target_mnt) / _canonical_path.relative_to(CANONICAL_ROOT)
    return _target_on_mnt


def prepare_dir(entry: DirTypedDict, *, target_mnt: StrOrPath) -> None:
    _target_on_mnt = fpath_on_target(entry["path"], target_mnt=target_mnt)
    try:
        _target_on_mnt.mkdir(exist_ok=True, parents=True)
        os.chown(_target_on_mnt, uid=entry["uid"], gid=entry["gid"])
        os.chmod(_target_on_mnt, mode=entry["mode"])
    except Exception as e:
        burst_suppressed_logger.exception(f"failed on preparing {entry!r}: {e!r}")
        raise


def prepare_non_regular(
    entry: NonRegularFileTypedDict, *, target_mnt: StrOrPath
) -> None:
    _target_on_mnt = fpath_on_target(entry["path"], target_mnt=target_mnt)
    try:
        if stat.S_ISLNK(entry["mode"]):
            _symlink_target_raw = entry["meta"]
            assert _symlink_target_raw, (
                f"{entry!r} is symlink, but no symlink target is defined"
            )

            _symlink_target = _symlink_target_raw.decode()
            _target_on_mnt.symlink_to(_symlink_target)

            # NOTE(20241213): chown will reset the sticky bit of the file!!!
            #   Remember to always put chown before chmod !!!
            os.chown(
                _target_on_mnt,
                uid=entry["uid"],
                gid=entry["gid"],
                follow_symlinks=False,
            )
            # NOTE: changing mode of symlink is not needed and uneffective, and on some platform
            #   changing mode of symlink will even result in exception raised.
            return

        # NOTE: legacy OTA image doesn't support char dev, so not process char device
        # NOTE: just ignore unknown entries
    except Exception as e:
        burst_suppressed_logger.exception(f"failed on preparing {entry!r}: {e!r}")
        raise


def prepare_regular(
    entry: RegularFileTypedDict,
    _rs: StrOrPath,
    *,
    target_mnt: StrOrPath,
    prepare_method: Literal["move", "hardlink", "copy"],
    hardlink_skip_apply_permission: bool = False,
) -> Path:
    """
    Returns:
        The path to the prepared file on target mount point.
    """
    _target_on_mnt = fpath_on_target(entry["path"], target_mnt=target_mnt)

    # NOTE(20241213): chown will reset the sticky bit of the file!!!
    #   Remember to always put chown before chmod !!!
    try:
        if prepare_method == "copy":
            shutil.copy(_rs, _target_on_mnt)
            os.chown(_target_on_mnt, uid=entry["uid"], gid=entry["gid"])
            os.chmod(_target_on_mnt, mode=entry["mode"])
            return _target_on_mnt

        if prepare_method == "hardlink":
            # NOTE: os.link will make dst a hardlink to src.
            os.link(_rs, _target_on_mnt)

            if not hardlink_skip_apply_permission:
                os.chown(_target_on_mnt, uid=entry["uid"], gid=entry["gid"])
                os.chmod(_target_on_mnt, mode=entry["mode"])
            return _target_on_mnt

        if prepare_method == "move":
            shutil.move(str(_rs), _target_on_mnt)
            os.chown(_target_on_mnt, uid=entry["uid"], gid=entry["gid"])
            os.chmod(_target_on_mnt, mode=entry["mode"])
            return _target_on_mnt
    except Exception as e:
        burst_suppressed_logger.exception(
            f"failed on preparing {entry!r}, {_rs=}: {e!r}"
        )
        raise
