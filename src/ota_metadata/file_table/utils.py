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

import contextlib
import logging
import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from typing import Literal, Optional, TypedDict

from simple_sqlite3_orm.utils import check_db_integrity, lookup_table

from ota_metadata.file_table import (
    FILE_TABLE_FNAME,
    FILE_TABLE_MEDIA_TYPE,
    FT_REGULAR_TABLE_NAME,
    FT_RESOURCE_TABLE_NAME,
    MEDIA_TYPE_FNAME,
)
from otaclient.configs.cfg import cfg
from otaclient_common._logging import get_burst_suppressed_logger
from otaclient_common._typing import StrOrPath
from otaclient_common.linux import copyfile_nocache

logger = logging.getLogger(__name__)
burst_suppressed_logger = get_burst_suppressed_logger(f"{__name__}.file_op_failed")


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
    _target_on_mnt = Path(target_mnt) / _canonical_path.relative_to(cfg.CANONICAL_ROOT)
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
            copyfile_nocache(_rs, _target_on_mnt)
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
            os.replace(str(_rs), _target_on_mnt)
            os.chown(_target_on_mnt, uid=entry["uid"], gid=entry["gid"])
            os.chmod(_target_on_mnt, mode=entry["mode"])
            return _target_on_mnt
    except Exception as e:
        burst_suppressed_logger.exception(
            f"failed on preparing {entry!r}, {_rs=}: {e!r}"
        )
        raise


#
# ------ save and load file_table ------ #
#
# Layout of /opt/ota/image-meta folder:
#   ./image-meta
#       - mediaType
#       - file_table.sqlite3
#
# When mediaType is "application/vnd.tier4.ota.file-based-ota-image.file_table.v1.sqlite3",
#   otaclient looks for `file_table.sqlite3` under this folder.


def _check_base_filetable(db_f: StrOrPath) -> StrOrPath | None:
    if not db_f or not Path(db_f).is_file():
        return

    with contextlib.suppress(Exception), contextlib.closing(
        sqlite3.connect(f"file:{db_f}?mode=ro&immutable=1", uri=True)
    ) as con:
        if not check_db_integrity(con):
            logger.warning(f"{db_f} fails integrity check")
            return

        if not (
            lookup_table(con, FT_REGULAR_TABLE_NAME)
            and lookup_table(con, FT_RESOURCE_TABLE_NAME)
        ):
            logger.warning(
                f"{db_f} presented, but either ft_regular or ft_resource tables missing"
            )
            return
    try:
        with contextlib.closing(sqlite3.connect(":memory:")) as con:
            con.execute(f"ATTACH '{db_f}' AS attach_test;")
            return db_f
    except Exception as e:
        logger.warning(f"{db_f} is valid, but cannot be attached: {e!r}, skip")


def save_fstable(
    db_f: StrOrPath,
    dst: StrOrPath,
    *,
    saved_name=FILE_TABLE_FNAME,
    media_type=FILE_TABLE_MEDIA_TYPE,
    media_type_fname=MEDIA_TYPE_FNAME,
) -> None:
    """Save the <db_f> to <dst>, with image-meta save layout."""

    dst = Path(dst)
    dst.mkdir(exist_ok=True, parents=True)

    with closing(sqlite3.connect(db_f)) as _fs_conn, closing(
        sqlite3.connect(dst / saved_name)
    ) as _dst_conn:
        with _dst_conn as conn:
            _fs_conn.backup(conn)

        with _dst_conn as conn:
            # change the journal_mode back to DELETE to make db file on read-only mount work.
            # see https://www.sqlite.org/wal.html#read_only_databases for more details.
            conn.execute("PRAGMA journal_mode=DELETE;")

    media_type_f = dst / media_type_fname
    media_type_f.write_text(media_type)


def find_saved_fstable(image_meta_dir: StrOrPath) -> StrOrPath | None:
    """Find and validate saved file_table in <image_meta_dir>.

    Returns:
        Return the file_table database fpath if it is a valid file_table.
    """
    image_meta_dir = Path(image_meta_dir)
    media_type_f = image_meta_dir / MEDIA_TYPE_FNAME

    if not (
        media_type_f.is_file() and media_type_f.read_text() == FILE_TABLE_MEDIA_TYPE
    ):
        logger.info(
            f"{MEDIA_TYPE_FNAME} not found under {image_meta_dir=}, "
            f"or mediaType is unsupported (supported type: {FILE_TABLE_MEDIA_TYPE=})"
        )
        return

    db_f = image_meta_dir / FILE_TABLE_FNAME
    if not db_f.is_file():
        logger.info(f"{db_f} not found under {image_meta_dir=}")
        return
    return _check_base_filetable(db_f)
