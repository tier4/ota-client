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
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Annotated, Optional

from multidict import CIMultiDict
from simple_sqlite3_orm import (
    AsyncORMBase,
    ConstrainRepr,
    CreateTableParams,
    ORMBase,
    ORMThreadPoolBase,
    TableSpec,
    gen_sql_stmt,
    utils,
)

from otaclient_common._typing import StrOrPath

from ._consts import HEADER_CONTENT_ENCODING, HEADER_OTA_FILE_CACHE_CONTROL
from .cache_control_header import export_kwargs_as_header_string
from .config import config as cfg
from .config import sqlite3_feature_flags

logger = logging.getLogger(__name__)

DB_TABLE_NAME = cfg.TABLE_NAME


class CacheMeta(TableSpec):
    """revision 4

    url: unquoted URL from the request of this cache entry.
    bucket_id: the LRU bucket this cache entry in.
    last_access: the UNIX timestamp of last access.
    cache_size: the file size of cached file(not the size of corresponding OTA file!).
    file_sha256:
        a. string of the sha256 hash of original OTA file(uncompressed),
        b. if a. is not available, then it is in form of "URL_<sha256_of_URL>".
    file_compression_alg: the compression used for the cached OTA file entry,
        if no compression is applied, then empty.
    content_encoding: the content_encoding header string comes with resp from remote server.
    """

    file_sha256: Annotated[str, ConstrainRepr("PRIMARY KEY")]
    url: Annotated[str, ConstrainRepr("NOT NULL")]
    bucket_idx: Annotated[int, ConstrainRepr("NOT NULL")] = 0
    last_access: Annotated[int, ConstrainRepr("NOT NULL")] = 0
    cache_size: Annotated[int, ConstrainRepr("NOT NULL")] = 0
    file_compression_alg: Optional[str] = None
    content_encoding: Optional[str] = None

    def __hash__(self) -> int:
        return hash(tuple(getattr(self, attrn) for attrn in self.model_fields))

    def export_headers_to_client(self) -> CIMultiDict[str]:
        """Export required headers for client.

        Currently includes content-type, content-encoding and ota-file-cache-control headers.
        """
        res = CIMultiDict()
        if self.content_encoding:
            res[HEADER_CONTENT_ENCODING] = self.content_encoding

        # export ota-file-cache-control headers if file_sha256 is valid file hash
        if self.file_sha256 and not self.file_sha256.startswith(
            cfg.URL_BASED_HASH_PREFIX
        ):
            res[HEADER_OTA_FILE_CACHE_CONTROL] = export_kwargs_as_header_string(
                file_sha256=self.file_sha256,
                file_compression_alg=self.file_compression_alg or "",
            )
        return res


class CacheMetaORM(ORMBase[CacheMeta]):
    orm_bootstrap_table_name = DB_TABLE_NAME
    orm_bootstrap_create_table_params = CreateTableParams(without_rowid=True)
    orm_bootstrap_indexes_params = [
        CacheMeta.table_create_index_stmt(
            table_name=DB_TABLE_NAME,
            index_name="bucket_idx_index",
            index_cols=("bucket_idx",),
            if_not_exists=True,
        ),
        CacheMeta.table_create_index_stmt(
            table_name=DB_TABLE_NAME,
            index_name="last_access_index",
            index_cols=("last_access",),
            if_not_exists=True,
        ),
    ]


class CacheMetaORMPool(ORMThreadPoolBase[CacheMeta]):
    orm_bootstrap_table_name = DB_TABLE_NAME
    bucket_fn, last_access_fn = "bucket_idx", "last_access"
    file_sha256_fn = "file_sha256"

    # fmt: off
    count_entries_with_limit = gen_sql_stmt(
        "SELECT", "count(*)", "FROM", orm_bootstrap_table_name,
        "WHERE", f"{bucket_fn}=:{bucket_fn}",
        "ORDER BY", last_access_fn,
        "LIMIT :limit"
    )
    select_entries_with_limit = gen_sql_stmt(
        "SELECT", "*", "FROM", orm_bootstrap_table_name,
        "WHERE", f"{bucket_fn}=:{bucket_fn}",
        "ORDER BY", last_access_fn,
        "LIMIT :limit"
    )
    delete_stmt = gen_sql_stmt(
        "DELETE", "FROM", orm_bootstrap_table_name,
        "WHERE", f"{bucket_fn}=:{bucket_fn}",
        "ORDER BY", last_access_fn,
        "LIMIT :limit"
    )
    rotate_stmt = gen_sql_stmt(
        "DELETE", "FROM", orm_bootstrap_table_name,
        "WHERE", file_sha256_fn, "IN", "(",
            "SELECT", file_sha256_fn, "FROM", orm_bootstrap_table_name,
            "WHERE", f"{bucket_fn}=:{bucket_fn}",
            "ORDER BY", last_access_fn,
            "LIMIT :limit",
        ")",
        "RETURNING", "*",
    )
    # fmt: on

    def _rotate_in_thread(self, bucket_idx: int, num: int) -> list[CacheMeta] | None:
        _params = {self.bucket_fn: bucket_idx, "limit": num}
        with self._thread_scope_orm._con as con:
            # check if we have enough entries to rotate
            cur = con.execute(self.count_entries_with_limit, _params)
            cur.row_factory = None
            if not (_raw_res := cur.fetchone()) or _raw_res[0] < num:
                return

            if not sqlite3_feature_flags.RETURNING_AVAILABLE:
                # first select entries met the requirements
                cur = con.execute(self.select_entries_with_limit, _params)
                rows_to_remove = list(cur)

                # delete the target entries
                con.execute(self.delete_stmt, _params)
                return rows_to_remove

            cur = con.execute(self.rotate_stmt, _params)
            return list(cur)

    def rotate_cache(self, bucket_idx: int, num: int) -> list[CacheMeta] | None:
        """
        NOTE: this is a blocking method.
        """
        return self._pool.submit(self._rotate_in_thread, bucket_idx, num).result()


class AsyncCacheMetaORM(AsyncORMBase[CacheMeta]):
    orm_bootstrap_table_name = DB_TABLE_NAME


def init_db(db_f: StrOrPath, table_name: str) -> None:
    """Init the database."""
    with closing(sqlite3.connect(db_f)) as con:
        orm = CacheMetaORM(con, table_name)
        orm.orm_bootstrap_db()
        utils.enable_wal_mode(con, relax_sync_mode=True)


def check_db(db_f: StrOrPath, table_name: str) -> bool:
    """Check whether specific db is normal or not."""
    if not Path(db_f).is_file():
        logger.warning(f"{db_f} not found, will init db")
        return False

    # NOTE(20250624): For sqlite3 on Ubuntu 20.04, the PRAGMA integrity_check
    #                 has an unexpected side_effect, which if the db file doesn't
    #                 exist, the integrity_check will CREATE a db file for it!
    #                 This unexpected behavior doesn't occur in Ubuntu 22.04.
    con = sqlite3.connect(f"file:{db_f}?mode=ro", uri=True)
    try:
        if not utils.check_db_integrity(con):
            logger.warning(f"{db_f} fails integrity check")
            return False
        if not utils.lookup_table(con, table_name):
            logger.warning(f"{table_name} not found in {db_f}")
            return False
    finally:
        con.close()
    return True
