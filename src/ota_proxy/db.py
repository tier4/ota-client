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

import asyncio
import logging
import sqlite3
from contextlib import closing
from typing import Optional

from multidict import CIMultiDict
from simple_sqlite3_orm import (
    AsyncORMBase,
    ConstrainRepr,
    CreateTableParams,
    ORMBase,
    TableSpec,
    gen_sql_stmt,
    utils,
)
from simple_sqlite3_orm.utils import check_db_integrity
from typing_extensions import Annotated

from otaclient_common._typing import StrOrPath

from ._consts import HEADER_CONTENT_ENCODING, HEADER_OTA_FILE_CACHE_CONTROL
from .cache_control_header import OTAFileCacheControl
from .config import config as cfg

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
            res[HEADER_OTA_FILE_CACHE_CONTROL] = (
                OTAFileCacheControl.export_kwargs_as_header(
                    file_sha256=self.file_sha256,
                    file_compression_alg=self.file_compression_alg or "",
                )
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


class AsyncCacheMetaORM(AsyncORMBase[CacheMeta]):
    bucket_fn, last_access_fn = "bucket_idx", "last_access"

    async def rotate_cache(
        self, bucket_idx: int, num: int
    ) -> Optional[list[CacheMeta]]:
        def _in_thread():
            _params = {self.bucket_fn: bucket_idx, "limit": num}

            with self._thread_scope_orm._con as con:
                # check if we have enough entries to rotate
                # fmt: off
                select_with_limit = gen_sql_stmt(
                    "SELECT", "count(*)", "FROM", self.orm_table_name,
                    "WHERE", f"{self.bucket_fn}=:{self.bucket_fn}",
                    "ORDER BY", self.last_access_fn,
                    "LIMIT :limit"
                )
                # fmt: on
                cur = con.execute(select_with_limit, _params)
                cur.row_factory = None
                # we don't have enough entries to delete
                if not (_raw_res := cur.fetchone()) or _raw_res[0] < num:
                    return

                # RETURNING statement is available only after sqlite3 v3.35.0
                if sqlite3.sqlite_version_info < (3, 35, 0):
                    # first select entries met the requirements
                    # fmt: off
                    select_to_delete_stmt = gen_sql_stmt(
                        "SELECT", "*", "FROM", self.orm_table_name,
                        "WHERE", f"{self.bucket_fn}=:{self.bucket_fn}",
                        "ORDER BY", self.last_access_fn,
                        "LIMIT :limit"
                    )
                    # fmt: on
                    cur = con.execute(select_to_delete_stmt, _params)
                    rows_to_remove = list(cur)

                    # delete the target entries
                    # fmt: off
                    delete_stmt = gen_sql_stmt(
                        "DELETE", "FROM", self.orm_table_name,
                        "WHERE", f"{self.bucket_fn}=:{self.bucket_fn}",
                        "ORDER BY", self.last_access_fn,
                        "LIMIT :limit"
                    )
                    # fmt: on
                    con.execute(delete_stmt, _params)

                    return rows_to_remove
                else:
                    # fmt: off
                    rotate_stmt = gen_sql_stmt(
                        "DELETE", "FROM", self.orm_table_name,
                        "WHERE", f"{self.bucket_fn}=:{self.bucket_fn}",
                        "RETURNING", "*",
                        "ORDER BY", self.last_access_fn,
                        "LIMIT :limit"
                    )
                    # fmt: on
                    cur = con.execute(rotate_stmt, _params)
                    return list(cur)

        return await asyncio.wrap_future(
            self._pool.submit(_in_thread),
            loop=self._loop,
        )


def init_db(db_f: StrOrPath, table_name: str) -> None:
    """Init the database."""
    with closing(sqlite3.connect(db_f)) as con:
        orm = CacheMetaORM(con, table_name)
        orm.orm_bootstrap_db()
        utils.enable_wal_mode(con, relax_sync_mode=True)


def check_db(db_f: StrOrPath, table_name: str) -> bool:
    with closing(sqlite3.connect(db_f)) as con:
        return check_db_integrity(con, table_name)
