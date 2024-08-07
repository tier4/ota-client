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
from pathlib import Path
from typing import Optional

from pydantic import SkipValidation
from simple_sqlite3_orm import (
    ConstrainRepr,
    ORMBase,
    TableSpec,
    TypeAffinityRepr,
    utils,
)
from simple_sqlite3_orm._orm import AsyncORMThreadPoolBase
from typing_extensions import Annotated

from otaclient_common.typing import StrOrPath

from ._consts import HEADER_CONTENT_ENCODING, HEADER_OTA_FILE_CACHE_CONTROL
from .cache_control_header import OTAFileCacheControl
from .config import config as cfg

logger = logging.getLogger(__name__)


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

    file_sha256: Annotated[
        str,
        TypeAffinityRepr(str),
        ConstrainRepr("PRIMARY KEY"),
        SkipValidation,
    ]
    url: Annotated[
        str,
        TypeAffinityRepr(str),
        ConstrainRepr("NOT NULL"),
        SkipValidation,
    ]
    bucket_idx: Annotated[
        int,
        TypeAffinityRepr(int),
        ConstrainRepr("NOT NULL"),
        SkipValidation,
    ] = 0
    last_access: Annotated[
        int,
        TypeAffinityRepr(int),
        ConstrainRepr("NOT NULL"),
        SkipValidation,
    ] = 0
    cache_size: Annotated[
        int,
        TypeAffinityRepr(int),
        ConstrainRepr("NOT NULL"),
        SkipValidation,
    ] = 0
    file_compression_alg: Annotated[
        Optional[str],
        TypeAffinityRepr(str),
        SkipValidation,
    ] = None
    content_encoding: Annotated[
        Optional[str],
        TypeAffinityRepr(str),
        SkipValidation,
    ] = None

    def __hash__(self) -> int:
        return hash(tuple(getattr(self, attrn) for attrn in self.model_fields))

    def export_headers_to_client(self) -> dict[str, str]:
        """Export required headers for client.

        Currently includes content-type, content-encoding and ota-file-cache-control headers.
        """
        res = {}
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


class CacheMetaORM(ORMBase[CacheMeta]): ...


class AsyncCacheMetaORM(AsyncORMThreadPoolBase[CacheMeta]):

    async def rotate_cache(
        self, bucket_idx: int, num: int
    ) -> Optional[list[CacheMeta]]:
        bucket_fn, last_access_fn = "bucket_idx", "last_access"

        def _inner():
            with self._con as con:
                # check if we have enough entries to rotate
                select_stmt = self.orm_table_spec.table_select_stmt(
                    select_from=self.orm_table_name,
                    select_cols="*",
                    function="count",
                    where_cols=(bucket_fn,),
                    order_by=(last_access_fn,),
                    limit=num,
                )
                cur = con.execute(select_stmt, {bucket_fn: bucket_idx})
                # we don't have enough entries to delete
                if not (_raw_res := cur.fetchone()) or _raw_res[0] < num:
                    return

                # RETURNING statement is available only after sqlite3 v3.35.0
                if sqlite3.sqlite_version_info < (3, 35, 0):
                    # first select entries met the requirements
                    select_to_delete_stmt = self.orm_table_spec.table_select_stmt(
                        select_from=self.orm_table_name,
                        where_cols=(bucket_fn,),
                        order_by=(last_access_fn,),
                        limit=num,
                    )
                    cur = con.execute(select_to_delete_stmt, {bucket_fn: bucket_idx})
                    rows_to_remove = list(cur)

                    # delete the target entries
                    delete_stmt = self.orm_table_spec.table_delete_stmt(
                        delete_from=self.orm_table_name,
                        where_cols=(bucket_fn,),
                        order_by=(last_access_fn,),
                        limit=num,
                    )
                    con.execute(delete_stmt, {bucket_fn: bucket_idx})

                    return rows_to_remove
                else:
                    rotate_stmt = self.orm_table_spec.table_delete_stmt(
                        delete_from=self.orm_table_name,
                        where_cols=(bucket_fn,),
                        order_by=(last_access_fn,),
                        limit=num,
                        returning_cols="*",
                    )
                    cur = con.execute(rotate_stmt, {bucket_fn: bucket_idx})
                    return list(cur)

        return await self._run_in_pool(_inner)


def check_db(db_f: StrOrPath, table_name: str) -> bool:
    """Check whether specific db is normal or not."""
    if not Path(db_f).is_file():
        logger.warning(f"{db_f} not found")
        return False

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


def init_db(db_f: StrOrPath, table_name: str) -> None:
    """Init the database."""
    con = sqlite3.connect(db_f)
    orm = CacheMetaORM(con, table_name)
    try:
        orm.orm_create_table(without_rowid=True)
        utils.enable_wal_mode(con, relax_sync_mode=True)
    finally:
        con.close()
