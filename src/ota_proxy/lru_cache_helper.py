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
import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional, Union

from simple_sqlite3_orm import utils

from .db import AsyncCacheMetaORM, CacheMeta

logger = logging.getLogger(__name__)


class LRUCacheHelper:
    """A helper class that provides API for accessing/managing cache entries in ota cachedb.

    Serveral buckets are created according to predefined file size threshould.
    Each bucket will maintain the cache entries of that bucket's size definition,
    LRU is applied on per-bucket scale.

    NOTE: currently entry in first bucket and last bucket will skip LRU rotate.
    """

    def __init__(
        self,
        db_f: Union[str, Path],
        *,
        bsize_dict: dict[int, int],
        table_name: str,
        thread_nums: int,
        thread_wait_timeout: int,
    ):
        self.bsize_list = list(bsize_dict)
        self.bsize_dict = bsize_dict.copy()

        def _con_factory():
            con = sqlite3.connect(
                db_f, check_same_thread=False, timeout=thread_wait_timeout
            )

            utils.enable_wal_mode(con, relax_sync_mode=True)
            utils.enable_mmap(con)
            utils.enable_tmp_store_at_memory(con)
            return con

        self._async_db = AsyncCacheMetaORM(
            table_name=table_name,
            con_factory=_con_factory,
            number_of_cons=thread_nums,
        )

        self._closed = False

    def close(self):
        self._async_db.orm_pool_shutdown(wait=True, close_connections=True)
        self._closed = True

    async def commit_entry(self, entry: CacheMeta) -> bool:
        """Commit cache entry meta to the database."""
        # populate bucket and last_access column
        entry.bucket_idx = bisect.bisect_right(self.bsize_list, entry.cache_size) - 1
        entry.last_access = int(time.time())

        if (await self._async_db.orm_insert_entry(entry, or_option="replace")) != 1:
            logger.error(f"db: failed to add {entry=}")
            return False
        return True

    async def lookup_entry(self, file_sha256: str) -> Optional[CacheMeta]:
        return await self._async_db.orm_select_entry(file_sha256=file_sha256)

    async def remove_entry(self, file_sha256: str) -> bool:
        return bool(await self._async_db.orm_delete_entries(file_sha256=file_sha256))

    async def rotate_cache(self, size: int) -> Optional[list[str]]:
        """Wrapper method for calling the database LRU cache rotating method.

        Args:
            size int: the size of file that we want to reserve space for

        Returns:
            A list of hashes that needed to be cleaned, or empty list if rotation
                is not required, or None if cache rotation cannot be executed.
        """
        # NOTE: currently item size smaller than 1st bucket and larger than latest bucket
        #       will be saved without cache rotating.
        if size >= self.bsize_list[-1] or size < self.bsize_list[1]:
            return []

        _cur_bucket_idx = bisect.bisect_right(self.bsize_list, size) - 1
        _cur_bucket_size = self.bsize_list[_cur_bucket_idx]

        # first: check the upper bucket, remove 1 item from any of the
        # upper bucket is enough.
        for _bucket_idx in range(_cur_bucket_idx + 1, len(self.bsize_list)):
            if res := await self._async_db.rotate_cache(_bucket_idx, 1):
                return [entry.file_sha256 for entry in res]

        # second: if cannot find one entry at any upper bucket, check current bucket
        res = await self._async_db.rotate_cache(
            _cur_bucket_idx, self.bsize_dict[_cur_bucket_size]
        )
        if res is None:
            return
        return [entry.file_sha256 for entry in res]
