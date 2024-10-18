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
"""Implementation of parsing ota metadata files and convert it to database."""


from __future__ import annotations

import sqlite3
from functools import partial
from pathlib import Path
from typing import Callable

import zstandard as zstd
from simple_sqlite3_orm import ORMBase
from simple_sqlite3_orm._table_spec import TableSpecType
from simple_sqlite3_orm.utils import (
    attach_database,
    check_db_integrity,
    enable_mmap,
    enable_tmp_store_at_memory,
    enable_wal_mode,
    lookup_table,
)

from ota_metadata._file_table.db import (
    DIR_TABLE_NAME,
    REGULARFILE_TABLE_NAME,
    SYMLINK_TABLE_NAME,
    init_filetable_db,
)
from ota_metadata._file_table.orm import DirectoriesORM, RegularFilesORM, SymlinksORM
from ota_metadata._file_table.tables import RegularFileTable
from ota_metadata.legacy.metafile_parser import (
    parse_dir_line,
    parse_regular_line,
    parse_symlink_line,
)
from ota_metadata.legacy.orm import ResourceTable, ResourceTableORM
from otaclient_common.typing import StrOrPath

BATCH_SIZE = 128
DIGEST_ALG = b"sha256"


def _import_from_metadatafiles(
    orm: ORMBase[TableSpecType],
    csv_txt: StrOrPath,
    *,
    parser_func: Callable[[str], TableSpecType],
):
    with open(csv_txt, "r") as f:
        _batch: list[TableSpecType] = []

        for line in f:
            _batch.append(parser_func(line))

            if len(_batch) >= BATCH_SIZE:
                _inserted = orm.orm_insert_entries(_batch, or_option="ignore")

                if _inserted != len(_batch):
                    raise ValueError(f"{csv_txt}: insert to database failed")
                _batch = []

        if _batch:
            orm.orm_insert_entries(_batch, or_option="ignore")


import_dirs_txt = partial(_import_from_metadatafiles, parser_func=parse_dir_line)
import_symlinks_txt = partial(
    _import_from_metadatafiles, parser_func=parse_symlink_line
)


def import_regulars_txt(
    reginf_orm: RegularFilesORM,
    resinf_orm: ResourceTableORM,
    csv_txt: StrOrPath,
    *,
    parser_func: Callable[
        [str], tuple[RegularFileTable, ResourceTable]
    ] = parse_regular_line,
):
    with open(csv_txt, "r") as f:
        _reginf_batch: list[RegularFileTable] = []
        _resinf_batch: list[ResourceTable] = []

        for line in f:
            _reg_inf, _res_inf = parser_func(line)
            _reginf_batch.append(_reg_inf)
            _resinf_batch.append(_res_inf)

            # NOTE: one file entry matches one resouce
            if len(_reginf_batch) >= BATCH_SIZE:
                _inserted = reginf_orm.orm_insert_entries(
                    _reginf_batch, or_option="ignore"
                )

                if _inserted != len(_reginf_batch):
                    raise ValueError("insert to database failed")
                _reginf_batch = []

                # NOTE: for duplicated resource insert, just ignore
                _inserted = resinf_orm.orm_insert_entries(
                    _resinf_batch, or_option="ignore"
                )
                if _inserted != len(_resinf_batch):
                    raise ValueError("insert to database failed")
                _resinf_batch = []

        if _reginf_batch:
            reginf_orm.orm_insert_entries(_reginf_batch, or_option="ignore")
        if _resinf_batch:
            resinf_orm.orm_insert_entries(_resinf_batch, or_option="ignore")


RESOURCETABLE_NAME = "resource_table"


def init_resourcetable_db(
    conn: sqlite3.Connection, *, schema_name: str | None = None
) -> None:
    res_orm = ResourceTableORM(conn, RESOURCETABLE_NAME, schema_name=schema_name)
    res_orm.orm_create_table()
    res_orm.orm_create_index(index_name="path_idx", index_keys=("path",))


def check_resourcetable_db(conn: sqlite3.Connection) -> bool:
    return check_db_integrity(conn) and lookup_table(conn, RESOURCETABLE_NAME)


FILETABLE_DB_FNAME = "file-table.sqlite3"
RESOURCETABLE_DB_FNAME = "resource-table.sqlite3"
FILETABLE_DB_ZST_FNAME = f"{FILETABLE_DB_FNAME}.zst"
RESOURCETABLE_DB_ZST_FNAME = f"{RESOURCETABLE_DB_FNAME}.zst"

RESOURCE_DB_SCHEMA_NAME = "resource_db"
ZST_COMPRESSION_LEVEL = 19


class OTAImageMetaDB:
    """Helper class for operating with OTA image metadata folder."""

    def __init__(self, meta_folder: StrOrPath) -> None:
        self.metadata_folder = meta_folder = Path(meta_folder)
        self.ftable_db = meta_folder / FILETABLE_DB_FNAME
        self.rtable_db = meta_folder / RESOURCETABLE_DB_FNAME

        self._connected: bool = False
        self._conn: sqlite3.Connection | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def conn(self) -> sqlite3.Connection | None:
        return self._conn

    def _connect_db(self) -> sqlite3.Connection:
        self._conn = conn = sqlite3.connect(self.ftable_db)
        attach_database(
            conn,
            str(self.rtable_db),
            schema_name=RESOURCE_DB_SCHEMA_NAME,
        )
        enable_mmap(conn)
        enable_wal_mode(conn)
        enable_tmp_store_at_memory(conn)

        return conn

    # APIs

    def export_db_zst(
        self,
        metadata_folder: StrOrPath,
        *,
        compression_level: int = ZST_COMPRESSION_LEVEL,
    ) -> str:
        """Export zst compressed db archive to <metadata_folder>.

        Export the db in current metadata working dir to a new location.
        """
        if self._connected:
            raise ValueError("cannot export db when db is opened")
        metadata_folder = Path(metadata_folder)

        _zst_ctx = zstd.ZstdCompressor(
            level=compression_level,
            write_checksum=True,
            threads=-1,
        )
        with open(self.ftable_db, "rb") as src, open(
            metadata_folder / FILETABLE_DB_ZST_FNAME, "wb"
        ) as dst:
            _zst_ctx.copy_stream(src, dst)
        with open(self.rtable_db, "rb") as src, open(
            metadata_folder / RESOURCETABLE_DB_ZST_FNAME, "wb"
        ) as dst:
            _zst_ctx.copy_stream(src, dst)

        return str(metadata_folder)

    def import_db_zst(self, metadata_folder: StrOrPath) -> str:
        """Import db from zst compressed archive at <metadata_folder>.

        Use the zst db archives at <metadata_folder> to setup the current metadata working dir.
        """
        if self._connected:
            raise ValueError("cannot import db when db is opened")
        metadata_folder = Path(metadata_folder)

        _zst_ctx = zstd.ZstdDecompressor()
        with open(metadata_folder / FILETABLE_DB_ZST_FNAME, "rb") as src, open(
            self.ftable_db, "wb"
        ) as dst:
            _zst_ctx.copy_stream(src, dst)
        with open(metadata_folder / RESOURCETABLE_DB_ZST_FNAME, "rb") as src, open(
            self.rtable_db, "wb"
        ) as dst:
            _zst_ctx.copy_stream(src, dst)

        return str(metadata_folder)

    def init_db(self) -> sqlite3.Connection:
        """Init and connect the database."""
        if self._connected:
            raise ValueError("cannot init db when db is connected")

        self.ftable_db.unlink(missing_ok=True)
        self.rtable_db.unlink(missing_ok=True)

        self._conn = conn = self._connect_db()
        self._connected = True
        init_filetable_db(conn)
        init_resourcetable_db(conn, schema_name=RESOURCE_DB_SCHEMA_NAME)

        return conn

    def connect_db(self) -> sqlite3.Connection:
        if self._connected:
            assert self._conn
            return self._conn
        return self._connect_db()

    def close_db(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
        self._connected = False


class OTAImageMetaORM:

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

        self.dir_orm = DirectoriesORM(conn, table_name=DIR_TABLE_NAME)
        self.symlink_orm = SymlinksORM(conn, table_name=SYMLINK_TABLE_NAME)
        self.regularfile_orm = RegularFilesORM(conn, table_name=REGULARFILE_TABLE_NAME)

        self.resource_orm = ResourceTableORM(
            conn,
            table_name=RESOURCETABLE_NAME,
            schema_name=RESOURCE_DB_SCHEMA_NAME,
        )

    def import_dir_list(self, csv: StrOrPath):
        import_dirs_txt(self.dir_orm, csv)

    def import_symlink_list(self, csv: StrOrPath):
        import_symlinks_txt(self.symlink_orm, csv)

    def import_regularfile_list(self, csv: StrOrPath):
        import_regulars_txt(
            reginf_orm=self.regularfile_orm,
            resinf_orm=self.resource_orm,
            csv_txt=csv,
        )
