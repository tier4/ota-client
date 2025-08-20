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

import sqlite3
import typing
from pathlib import Path
from typing import Callable, Generator, Optional, TypedDict

from pydantic import SkipValidation
from simple_sqlite3_orm import (
    ConstrainRepr,
    CreateIndexParams,
    CreateTableParams,
    ORMBase,
    ORMThreadPoolBase,
    TableSpec,
    gen_sql_stmt,
)
from simple_sqlite3_orm.utils import enable_mmap, enable_wal_mode
from typing_extensions import Annotated

from ota_metadata.file_table import (
    FT_DIR_TABLE_NAME,
    FT_INODE_TABLE_NAME,
    FT_NON_REGULAR_TABLE_NAME,
    FT_REGULAR_TABLE_NAME,
    FT_RESOURCE_TABLE_NAME,
)
from otaclient_common._logging import get_burst_suppressed_logger

from .utils import DirTypedDict, NonRegularFileTypedDict, RegularFileTypedDict

burst_suppressed_logger = get_burst_suppressed_logger(f"{__name__}.file_op_failed")

MAX_ENTRIES_PER_DIGEST = 16
DB_TIMEOUT = 16

# ------ inode table ------ #


class FileTableInode(TableSpec):
    inode_id: Annotated[int, ConstrainRepr("PRIMARY KEY"), SkipValidation]
    uid: Annotated[int, ConstrainRepr("NOT NULL"), SkipValidation]
    gid: Annotated[int, ConstrainRepr("NOT NULL"), SkipValidation]
    mode: Annotated[int, ConstrainRepr("NOT NULL"), SkipValidation]
    links_count: Annotated[Optional[int], SkipValidation] = None
    # NOTE: due to legacy OTA image doesn't support xattrs, we
    #       just don't use this field for now.
    xattrs: Annotated[Optional[bytes], SkipValidation] = None


class FiletableInodeTypedDict(TypedDict, total=False):
    inode_id: int
    uid: int
    gid: int
    mode: int
    links_count: Optional[int]
    xattrs: Optional[bytes]


class FileTableInodeORM(ORMBase[FileTableInode]):
    orm_bootstrap_table_name = FT_INODE_TABLE_NAME
    orm_bootstrap_create_table_params = CreateTableParams(without_rowid=False)


# ------ regular file table ------ #


class FileTableRegularFiles(TableSpec):
    """DB table for regular file entries."""

    path: Annotated[str, ConstrainRepr("PRIMARY KEY"), SkipValidation]
    inode_id: Annotated[int, ConstrainRepr("NOT NULL"), SkipValidation]
    resource_id: Annotated[int, SkipValidation]


class FileTableRegularTypedDict(TypedDict, total=False):
    path: str
    inode_id: int
    resource_id: int


class FileTableRegularORM(ORMBase[FileTableRegularFiles]):
    orm_bootstrap_table_name = FT_REGULAR_TABLE_NAME
    orm_bootstrap_create_table_params = CreateTableParams(without_rowid=True)
    orm_bootstrap_indexes_params = [
        CreateIndexParams(
            index_name="fr_resource_id_index", index_cols=("resource_id",)
        ),
        CreateIndexParams(index_name="fr_inode_id_index", index_cols=("inode_id",)),
    ]


class FileTableRegularORMPool(ORMThreadPoolBase[FileTableRegularFiles]):
    orm_bootstrap_table_name = FT_REGULAR_TABLE_NAME


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


class FileTableNonRegularTypedDict(TypedDict, total=False):
    path: str
    inode_id: int
    meta: Optional[bytes]


class FileTableNonRegularORM(ORMBase[FileTableNonRegularFiles]):
    orm_bootstrap_table_name = FT_NON_REGULAR_TABLE_NAME
    orm_bootstrap_create_table_params = CreateTableParams(without_rowid=True)
    orm_bootstrap_indexes_params = [
        CreateIndexParams(index_name="fnr_inode_id_index", index_cols=("inode_id",)),
    ]


# ------ directory table ------ #


class FileTableDirectories(TableSpec):
    path: Annotated[str, ConstrainRepr("PRIMARY KEY"), SkipValidation]
    inode_id: Annotated[int, ConstrainRepr("NOT NULL"), SkipValidation]


class FileTableDirectoryTypedDict(TypedDict, total=False):
    path: str
    inode_id: int


class FileTableDirORM(ORMBase[FileTableDirectories]):
    orm_bootstrap_table_name = FT_DIR_TABLE_NAME
    orm_bootstrap_create_table_params = CreateTableParams(without_rowid=True)
    orm_bootstrap_indexes_params = [
        CreateIndexParams(index_name="fd_inode_id_index", index_cols=("inode_id",)),
    ]


class FileTableDirORMPool(ORMThreadPoolBase[FileTableDirectories]):
    orm_bootstrap_table_name = FT_DIR_TABLE_NAME
    orm_bootstrap_create_table_params = CreateTableParams(without_rowid=True)
    orm_bootstrap_indexes_params = [
        CreateIndexParams(index_name="fd_inode_id_index", index_cols=("inode_id",)),
    ]


# ------ resource table ------ #


class FileTableResource(TableSpec):
    resource_id: Annotated[int, ConstrainRepr("PRIMARY KEY"), SkipValidation]
    digest: Annotated[bytes, ConstrainRepr("NOT NULL"), SkipValidation]
    size: Annotated[int, ConstrainRepr("NOT NULL"), SkipValidation]
    contents: Annotated[Optional[bytes], SkipValidation] = None


class FileTableResourceTypedDict(TypedDict, total=False):
    resource_id: int
    digest: bytes
    size: int
    contents: Optional[bytes]


class FileTableResourceORM(ORMBase[FileTableResource]):
    orm_bootstrap_table_name = FT_RESOURCE_TABLE_NAME
    orm_bootstrap_create_table_params = CreateTableParams(without_rowid=False)
    orm_bootstrap_indexes_params = [
        CreateIndexParams(index_name="frs_digest_index", index_cols=("digest",))
    ]


class FileTableResourceORMPool(ORMThreadPoolBase[FileTableResource]):
    orm_bootstrap_table_name = FT_RESOURCE_TABLE_NAME


#
# ------ file_table helper ------ #
#


class FileTableDBHelper:
    # NOTE(20250604): filter out the empty file.
    # NOTE(20250724): if the entry is inlined, we don't need to select them.
    ITER_COMMON_DIGEST = gen_sql_stmt(
        f"SELECT base.{FT_REGULAR_TABLE_NAME}.path, base.{FT_RESOURCE_TABLE_NAME}.digest",
        f"FROM base.{FT_REGULAR_TABLE_NAME}",
        f"JOIN base.{FT_RESOURCE_TABLE_NAME} USING(resource_id)",
        f"JOIN {FT_RESOURCE_TABLE_NAME} AS target_rs ON base.{FT_RESOURCE_TABLE_NAME}.digest = target_rs.digest",
        f"WHERE base.{FT_RESOURCE_TABLE_NAME}.size != 0 AND target_rs.contents IS NULL",
        f"ORDER BY base.{FT_RESOURCE_TABLE_NAME}.digest",
    )

    def __init__(self, db_f: str | Path) -> None:
        self.db_f = db_f

    def connect_fstable_db(
        self, *, enable_wal: bool = False, enable_mmap_size: int | None = None
    ) -> sqlite3.Connection:
        _conn = sqlite3.connect(self.db_f, check_same_thread=False, timeout=DB_TIMEOUT)
        if enable_wal:
            enable_wal_mode(_conn)
        if enable_mmap_size and enable_mmap_size > 0:
            enable_mmap(_conn, enable_mmap_size)
        return _conn

    def select_all_digests_with_size(
        self, *, exclude_inlined: bool = True
    ) -> Generator[tuple[bytes, int]]:
        """Select all unique digests of this file_table, with their size."""
        stmt = f"SELECT digest,size FROM {FT_RESOURCE_TABLE_NAME}"
        if exclude_inlined:
            stmt = f"SELECT digest,size FROM {FT_RESOURCE_TABLE_NAME} WHERE contents IS NULL AND size!=0"

        with FileTableDirORM(self.connect_fstable_db()) as orm:
            yield from orm.orm_select_entries(
                _row_factory=typing.cast(
                    "Callable[..., tuple[bytes, int]]", sqlite3.Row
                ),
                _stmt=stmt,
            )

    def iter_dir_entries(self) -> Generator[DirTypedDict]:
        with FileTableDirORM(self.connect_fstable_db()) as orm:
            _row_factory = typing.cast(Callable[..., DirTypedDict], sqlite3.Row)

            # fmt: off
            yield from orm.orm_select_entries(
                _row_factory=_row_factory,
                _stmt = gen_sql_stmt(
                    "SELECT", "path,uid,gid,mode,xattrs",
                    "FROM", FT_DIR_TABLE_NAME,
                    "JOIN", FT_INODE_TABLE_NAME, "USING(inode_id)",
                )
            )
            # fmt: on

    def iter_regular_entries(self) -> Generator[RegularFileTypedDict]:
        with FileTableRegularORM(self.connect_fstable_db()) as orm:
            # fmt: off
            _stmt = gen_sql_stmt(
                "SELECT", "path,uid,gid,mode,links_count,xattrs,digest,size,inode_id,contents",
                "FROM", FT_REGULAR_TABLE_NAME,
                "JOIN", FT_INODE_TABLE_NAME, "USING(inode_id)",
                "JOIN", FT_RESOURCE_TABLE_NAME, "USING(resource_id)",
            )
            # fmt: on
            yield from orm.orm_select_entries(
                _row_factory=typing.cast(
                    "Callable[..., RegularFileTypedDict]", sqlite3.Row
                ),
                _stmt=_stmt,
            )

    def iter_non_regular_entries(self) -> Generator[NonRegularFileTypedDict]:
        with FileTableNonRegularORM(self.connect_fstable_db()) as orm:
            # fmt: off
            yield from orm.orm_select_entries(
                _row_factory=typing.cast(
                    Callable[..., NonRegularFileTypedDict], sqlite3.Row
                ),
                _stmt=gen_sql_stmt(
                    "SELECT", "path,uid,gid,mode,xattrs,meta",
                    "FROM", FT_NON_REGULAR_TABLE_NAME,
                    "JOIN", FT_INODE_TABLE_NAME, "USING(inode_id)",
                )
            )
            # fmt: on

    def iter_common_regular_entries_by_digest(
        self,
        base_file_table: str,
        *,
        max_num_of_entries_per_digest: int = MAX_ENTRIES_PER_DIGEST,
    ) -> Generator[tuple[bytes, list[Path]]]:
        _hash = b""
        _cur: list[Path] = []

        with FileTableRegularORM(self.connect_fstable_db()) as orm:
            orm.orm_con.execute(f"ATTACH DATABASE '{base_file_table}' AS base;")
            for entry in orm.orm_select_entries(
                _row_factory=sqlite3.Row,
                _stmt=self.ITER_COMMON_DIGEST,
            ):
                _this_digest: bytes = entry["digest"]
                _this_path: Path = Path(entry["path"])

                if _this_digest == _hash:
                    # When there are too many entries for this digest, just pick the first
                    #   <max_num_of_entries_per_digest> of them.
                    if len(_cur) <= max_num_of_entries_per_digest:
                        _cur.append(_this_path)
                else:
                    if _cur:
                        yield _hash, _cur
                    _hash, _cur = _this_digest, [_this_path]

            if _cur:
                yield _hash, _cur

    def get_dir_orm_pool(self, db_conn_num: int) -> FileTableDirORMPool:
        return FileTableDirORMPool(
            con_factory=self.connect_fstable_db,
            number_of_cons=db_conn_num,
        )

    def get_dir_orm(self, conn: sqlite3.Connection | None = None) -> FileTableDirORM:
        if conn is not None:
            return FileTableDirORM(conn)
        return FileTableDirORM(self.connect_fstable_db())

    def get_regular_file_orm_pool(self, db_conn_num: int) -> FileTableRegularORMPool:
        return FileTableRegularORMPool(
            con_factory=self.connect_fstable_db, number_of_cons=db_conn_num
        )

    def get_regular_file_orm(
        self, conn: sqlite3.Connection | None = None
    ) -> FileTableRegularORM:
        if conn is not None:
            return FileTableRegularORM(conn)
        return FileTableRegularORM(self.connect_fstable_db())

    def get_non_regular_file_orm(
        self, conn: sqlite3.Connection | None = None
    ) -> FileTableNonRegularORM:
        if conn is not None:
            return FileTableNonRegularORM(conn)
        return FileTableNonRegularORM(self.connect_fstable_db())

    def get_inode_orm(
        self, conn: sqlite3.Connection | None = None
    ) -> FileTableInodeORM:
        if conn is not None:
            return FileTableInodeORM(conn)
        return FileTableInodeORM(self.connect_fstable_db())

    def get_resource_orm(
        self, conn: sqlite3.Connection | None = None
    ) -> FileTableResourceORM:
        if conn is not None:
            return FileTableResourceORM(conn)
        return FileTableResourceORM(self.connect_fstable_db())
