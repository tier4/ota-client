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

import bisect
import logging
import random
import sqlite3

import pytest
import pytest_asyncio
from simple_sqlite3_orm import ORMBase

from ota_proxy import config as cfg
from ota_proxy.db import CacheMeta
from ota_proxy.ota_cache import LRUCacheHelper
from ota_proxy.utils import url_based_hash

logger = logging.getLogger(__name__)

TEST_DATA_SET_SIZE = 4096
TEST_LOOKUP_ENTRIES = 1200
TEST_DELETE_ENTRIES = 512


class OTACacheDB(ORMBase[CacheMeta]):
    pass


@pytest.fixture(autouse=True, scope="module")
def setup_testdata() -> dict[str, CacheMeta]:
    size_list = list(cfg.BUCKET_FILE_SIZE_DICT)

    entries: dict[str, CacheMeta] = {}
    for idx in range(TEST_DATA_SET_SIZE):
        target_size = random.choice(size_list)
        mocked_url = f"#{idx}/w/targetsize={target_size}"
        file_sha256 = url_based_hash(mocked_url)

        entries[file_sha256] = CacheMeta(
            url=mocked_url,
            # see lru_cache_helper module for more details
            bucket_idx=bisect.bisect_right(size_list, target_size),
            cache_size=target_size,
            file_sha256=file_sha256,
        )
    return entries


@pytest.fixture(autouse=True, scope="module")
def entries_to_lookup(setup_testdata: dict[str, CacheMeta]) -> list[CacheMeta]:
    return random.sample(
        list(setup_testdata.values()),
        k=TEST_LOOKUP_ENTRIES,
    )


@pytest.fixture(autouse=True, scope="module")
def entries_to_remove(setup_testdata: dict[str, CacheMeta]) -> list[CacheMeta]:
    return random.sample(
        list(setup_testdata.values()),
        k=TEST_DELETE_ENTRIES,
    )


@pytest.mark.asyncio(scope="class")
class TestLRUCacheHelper:

    @pytest_asyncio.fixture(autouse=True, scope="class")
    async def lru_helper(self, tmp_path_factory: pytest.TempPathFactory):
        ota_cache_folder = tmp_path_factory.mktemp("ota-cache")
        db_f = ota_cache_folder / "db_f"

        # init table
        conn = sqlite3.connect(db_f)
        orm = OTACacheDB(conn, cfg.TABLE_NAME)
        orm.orm_create_table(without_rowid=True)
        conn.close()

        lru_cache_helper = LRUCacheHelper(
            db_f,
            bsize_dict=cfg.BUCKET_FILE_SIZE_DICT,
            table_name=cfg.TABLE_NAME,
            thread_nums=cfg.DB_THREADS,
            thread_wait_timeout=cfg.DB_THREAD_WAIT_TIMEOUT,
        )
        try:
            yield lru_cache_helper
        finally:
            lru_cache_helper.close()

    async def test_commit_entry(
        self, lru_helper: LRUCacheHelper, setup_testdata: dict[str, CacheMeta]
    ):
        for _, entry in setup_testdata.items():
            # deliberately clear the bucket_idx, this should be set by commit_entry method
            _copy = entry.model_copy()
            _copy.bucket_idx = 0
            assert await lru_helper.commit_entry(entry)

    async def test_lookup_entry(
        self,
        lru_helper: LRUCacheHelper,
        entries_to_lookup: list[CacheMeta],
        setup_testdata: dict[str, CacheMeta],
    ):
        for entry in entries_to_lookup:
            assert (
                await lru_helper.lookup_entry(entry.file_sha256)
                == setup_testdata[entry.file_sha256]
            )

    async def test_remove_entry(
        self, lru_helper: LRUCacheHelper, entries_to_remove: list[CacheMeta]
    ):
        for entry in entries_to_remove:
            assert await lru_helper.remove_entry(entry.file_sha256)

    async def test_rotate_cache(self, lru_helper: LRUCacheHelper):
        """Ensure the LRUHelper properly rotates the cache entries.

        We should file enough entries into the database, so each rotate should be successful.
        """
        # NOTE that the first bucket and last bucket will not be rotated,
        #   see lru_cache_helper module for more details.
        for target_bucket in list(cfg.BUCKET_FILE_SIZE_DICT)[1:-1]:
            entries_to_be_removed = await lru_helper.rotate_cache(target_bucket)
            assert entries_to_be_removed is not None and len(entries_to_be_removed) != 0
