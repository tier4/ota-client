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
"""Implementation of OTA cache control."""

from __future__ import annotations

import bisect
import sqlite3
import time
from functools import partial

from anyio.to_thread import run_sync
from simple_sqlite3_orm import utils

from otaclient_common._logging import get_burst_suppressed_logger
from otaclient_common._typing import StrOrPath

from .config import config as cfg
from .db import AsyncCacheMetaORM, CacheMeta, CacheMetaORMPool

burst_suppressed_logger = get_burst_suppressed_logger(f"{__name__}.db_error")


def _con_factory(db_f: StrOrPath, thread_wait_timeout: int):
    con = sqlite3.connect(db_f, check_same_thread=False, timeout=thread_wait_timeout)

    utils.enable_wal_mode(con, relax_sync_mode=True)
    utils.enable_mmap(con)
    utils.enable_tmp_store_at_memory(con)
    return con


class LRUCacheHelper:
    """A helper class that provides API for accessing/managing cache entries in ota cachedb.

    Serveral buckets are created according to predefined file size threshould.
    Each bucket will maintain the cache entries of that bucket's size definition,
    LRU is applied on per-bucket scale.

    NOTE: currently entry in first bucket and last bucket will skip LRU rotate.
    """

    def __init__(
        self,
        db_f: StrOrPath,
        *,
        bsize_dict: dict[int, int] = cfg.BUCKET_FILE_SIZE_DICT,
        table_name: str = cfg.TABLE_NAME,
        read_db_thread_nums: int = cfg.READ_DB_THREADS,
        write_db_thread_nums: int = cfg.WRITE_DB_THREAD,
        thread_wait_timeout: int = cfg.DB_THREAD_WAIT_TIMEOUT,
    ):
        self.bsize_list = list(bsize_dict)
        self.bsize_dict = bsize_dict.copy()

        _configured_con_factory = partial(_con_factory, db_f, thread_wait_timeout)
        self._async_db = AsyncCacheMetaORM(
            table_name=table_name,
            con_factory=_configured_con_factory,
            number_of_cons=read_db_thread_nums,
            row_factory="table_spec_no_validation",
        )
        self._db = CacheMetaORMPool(
            table_name=table_name,
            con_factory=_configured_con_factory,
            number_of_cons=write_db_thread_nums,
            row_factory="table_spec_no_validation",
        )

        self._closed = False

    def _close(self) -> None:
        self._async_db.orm_pool_shutdown(wait=True, close_connections=True)
        self._db.orm_pool_shutdown(wait=True, close_connections=True)

    # sync API

    def commit_entry(self, entry: CacheMeta) -> bool:
        """Commit cache entry meta to the database."""
        # populate bucket and last_access column
        entry.bucket_idx = bisect.bisect_right(self.bsize_list, entry.cache_size) - 1
        entry.last_access = int(time.time())

        if (self._db.orm_insert_entry(entry, or_option="replace")) != 1:
            burst_suppressed_logger.error(f"db: failed to add {entry=}")
            return False
        return True

    def rotate_cache(self, size: int) -> list[str] | None:
        """Wrapper method for calling the database LRU cache rotating method.

        Args:
            size int: the size of file that we want to reserve space for

        Returns:
            A list of hashes that needed to be cleaned, or empty list if rotation
                is not required, or None if cache rotation cannot be executed.
        """
        # NOTE: currently item size smaller than 1st bucket and larger than latest bucket
        #       will be saved without cache rotating.
        # NOTE: return [] to indicate caller that rotate_cache is successful.
        if size >= self.bsize_list[-1] or size < self.bsize_list[1]:
            return []

        _cur_bucket_idx = bisect.bisect_right(self.bsize_list, size) - 1
        _cur_bucket_size = self.bsize_list[_cur_bucket_idx]

        # first: check the upper bucket, remove 1 item from any of the
        # upper bucket is enough.
        # NOTE(20240802): limit the max steps we can go to avoid remove too large file
        max_steps = min(len(self.bsize_list), _cur_bucket_idx + 3)
        for _bucket_idx in range(_cur_bucket_idx + 1, max_steps):
            if res := self._db.rotate_cache(_bucket_idx, 1):
                return [entry.file_sha256 for entry in res]

        # second: if cannot find one entry at any upper bucket, check current bucket
        if res := self._db.rotate_cache(
            _cur_bucket_idx, self.bsize_dict[_cur_bucket_size]
        ):
            return [entry.file_sha256 for entry in res]

    # async API

    async def close(self) -> None:
        self._closed = True
        await run_sync(self._close)

    async def lookup_entry(self, file_sha256: str) -> CacheMeta | None:
        return await self._async_db.orm_select_entry(file_sha256=file_sha256)

    async def remove_entry(self, file_sha256: str) -> bool:
        return await self._async_db.orm_delete_entries(file_sha256=file_sha256) > 0
