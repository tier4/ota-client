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

import os
import shutil
import stat
from pathlib import Path
from typing import Literal, Optional, TypedDict

from pydantic import SkipValidation
from simple_sqlite3_orm import (
    ConstrainRepr,
    CreateIndexParams,
    CreateTableParams,
    ORMBase,
    TableSpec,
)
from typing_extensions import Annotated

from otaclient_common._logging import get_burst_suppressed_logger
from otaclient_common._typing import StrOrPath

burst_suppressed_logger = get_burst_suppressed_logger(f"{__name__}.file_op_failed")

CANONICAL_ROOT = "/"

FT_REGULAR_TABLE_NAME = "ft_regular"
FT_NON_REGULAR_TABLE_NAME = "ft_non_regular"
FT_DIR_TABLE_NAME = "ft_dir"
FT_INODE_TABLE_NAME = "ft_inode"
FT_RESOURCE_TABLE_NAME = "ft_resource"
MAX_ENTRIES_PER_DIGEST = 10

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
            assert (
                _symlink_target_raw
            ), f"{entry!r} is symlink, but no symlink target is defined"

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
) -> None:
    _target_on_mnt = fpath_on_target(entry["path"], target_mnt=target_mnt)

    # NOTE(20241213): chown will reset the sticky bit of the file!!!
    #   Remember to always put chown before chmod !!!
    try:
        if prepare_method == "copy":
            shutil.copy(_rs, _target_on_mnt)
            os.chown(_target_on_mnt, uid=entry["uid"], gid=entry["gid"])
            os.chmod(_target_on_mnt, mode=entry["mode"])
            return

        if prepare_method == "hardlink":
            # NOTE: os.link will make dst a hardlink to src.
            os.link(_rs, _target_on_mnt)
            # NOTE: although we actually don't need to set_perm and set_xattr everytime
            #   to file paths point to the same inode, for simplicity here we just
            #   do it everytime.
            os.chown(_target_on_mnt, uid=entry["uid"], gid=entry["gid"])
            os.chmod(_target_on_mnt, mode=entry["mode"])
            return

        if prepare_method == "move":
            shutil.move(str(_rs), _target_on_mnt)
            os.chown(_target_on_mnt, uid=entry["uid"], gid=entry["gid"])
            os.chmod(_target_on_mnt, mode=entry["mode"])
    except Exception as e:
        burst_suppressed_logger.exception(
            f"failed on preparing {entry!r}, {_rs=}: {e!r}"
        )
        raise


#
# ------ typeddicts ------ #
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


class DirTypedDict(FileTableEntryTypedDict): ...


class NonRegularFileTypedDict(FileTableEntryTypedDict):
    meta: Optional[bytes]


#
# ------ table defs and ORMs ------ #
#

# ------ inode table ------ #

class FileTableInode(TableSpec):
    inode_id: int
    uid: int
    gid: int
    mode: int
    links_count: Optional[int] = None
    # NOTE: due to legacy OTA image doesn't support xattrs, we
    #       just don't use this field for now.
    xattrs: Optional[bytes] = None

class FileTableInodeORM(ORMBase[FileTableInode]):
    orm_bootstrap_table_name = FT_INODE_TABLE_NAME
    orm_bootstrap_create_table_params = CreateTableParams(without_rowid=True)

# ------ regular file table ------ #

class FileTableRegularFiles(TableSpec):
    """DB table for regular file entries."""

    path: Annotated[str, ConstrainRepr("PRIMARY KEY"), SkipValidation]
    inode_id: Annotated[int, ConstrainRepr("NOT NULL"), SkipValidation]
    resource_id: Annotated[int, SkipValidation]

class FileTableRegularORM(ORMBase[FileTableRegularFiles]):

    orm_bootstrap_table_name = FT_REGULAR_TABLE_NAME
    orm_bootstrap_create_table_params = CreateTableParams(without_rowid=True)
    orm_bootstrap_indexes_params = [
        CreateIndexParams(index_name="resource_id_index", index_cols=("resource_id",)),
        CreateIndexParams(index_name="inode_id_index", index_cols=("inode_id",)),
    ]

# ------ non-regular file table ------ #

class FileTableNonRegularFiles(TableSpec):
    """DB table for non-regular file entries.

    This includes:
    1. symlink.
    2. chardev file.

    NOTE that support for chardev file is only for overlayfs' whiteout file,
        so only device num as 0,0 will be allowed.
    NOTE: chardev is not supported by legacy OTA image, so just ignore it.
    """

    path: Annotated[str, ConstrainRepr("PRIMARY KEY"), SkipValidation]
    inode_id: Annotated[int, ConstrainRepr("NOT NULL"), SkipValidation]
    meta: Annotated[Optional[bytes], SkipValidation] = None
    """The contents of the file. Currently only used by symlink."""

class FileTableNonRegularORM(ORMBase[FileTableNonRegularFiles]):

    orm_bootstrap_table_name = FT_NON_REGULAR_TABLE_NAME
    orm_bootstrap_create_table_params = CreateTableParams(without_rowid=True)
    orm_bootstrap_indexes_params = [
        CreateIndexParams(index_name="inode_id_index", index_cols=("inode_id",)),
    ]

# ------ directory table ------ #

class FileTableDirectories(TableSpec):

    path: Annotated[str, ConstrainRepr("PRIMARY KEY"), SkipValidation]
    inode_id: Annotated[int, ConstrainRepr("NOT NULL"), SkipValidation]

class FileTableDirORM(ORMBase[FileTableDirectories]):

    orm_bootstrap_table_name = FT_DIR_TABLE_NAME
    orm_bootstrap_create_table_params = CreateTableParams(without_rowid=True)
    orm_bootstrap_indexes_params = [
        CreateIndexParams(index_name="inode_id_index", index_cols=("inode_id",)),
    ]

# ------ resource table ------ #

class FileTableResource(TableSpec):

    resource_id: Annotated[int, ConstrainRepr("PRIMARY KEY"), SkipValidation]
    digest: Annotated[bytes, ConstrainRepr("NOT NULL"), SkipValidation]
    size: Annotated[int, ConstrainRepr("NOT NULL"), SkipValidation]

class FileTableResourceORM(ORMBase[FileTableResource]):

    orm_bootstrap_table_name = FT_RESOURCE_TABLE_NAME
    orm_bootstrap_create_table_params = CreateTableParams(without_rowid=True)
    orm_bootstrap_indexes_params = [
        CreateIndexParams(index_name="digest_index", index_cols=("digest",))
    ]
