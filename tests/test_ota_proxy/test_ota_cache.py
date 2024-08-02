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
import bisect
import logging
import random
import sqlite3
from pathlib import Path
from typing import Coroutine, Optional

import pytest
import pytest_asyncio
from simple_sqlite3_orm import ORMBase

from ota_proxy import config as cfg
from ota_proxy.db import CacheMeta
from ota_proxy.ota_cache import CachingRegister, LRUCacheHelper
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


class TestOngoingCachingRegister:
    """
    Testing multiple access to a single resource at the same time with ongoing_cache control.

    NOTE; currently this test only testing the weakref implementation part,
          the file descriptor management part is tested in test_ota_proxy_server
    """

    URL = "common_url"
    WORKS_NUM = 128

    @pytest.fixture(autouse=True)
    async def setup_test(self, tmp_path: Path):
        self.base_dir = tmp_path / "base_dir"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.register = CachingRegister(self.base_dir)

        # events
        # NOTE: we don't have Barrier in asyncio lib, so
        #       use Semaphore to simulate one
        self.register_finish = asyncio.Semaphore(self.WORKS_NUM)
        self.sync_event = asyncio.Event()
        self.writer_done_event = asyncio.Event()

    async def _wait_for_registeration_finish(self):
        while not self.register_finish.locked():
            await asyncio.sleep(0.16)
        logger.info("registeration finished")

    async def _worker(
        self,
        idx: int,
    ) -> tuple[bool, Optional[CacheMeta]]:
        """
        Returns tuple of bool indicates whether the worker is writter, and CacheMeta
        from tracker.
        """
        # simulate multiple works subscribing the register
        await self.sync_event.wait()
        await asyncio.sleep(random.randrange(100, 200) // 100)

        _tracker, _is_writer = await self.register.get_tracker(
            self.URL,
            executor=None,  # type: ignore
            callback=None,  # type: ignore
            below_hard_limit_event=None,  # type: ignore
        )
        await self.register_finish.acquire()

        if _is_writer:
            logger.info(f"#{idx} is provider")
            # NOTE: use last_access field to store worker index
            # NOTE 2: bypass provider_start method, directly set tracker property
            _tracker.meta = CacheMeta(
                last_access=idx,
                url="some_url",
                file_sha256="some_filesha256_value",
            )
            _tracker._writer_ready.set()
            # simulate waiting for writer finished downloading
            await self.writer_done_event.wait()
            logger.info(f"writer #{idx} finished")
            # finished
            _tracker._writer_finished.set()
            _tracker._ref = None
            return True, _tracker.meta
        else:
            logger.debug(f"#{idx} is subscriber")
            _tracker._subscriber_ref_holder.append(_tracker._ref)
            while not _tracker.writer_finished:  # simulating cache streaming
                await asyncio.sleep(0.1)
            # NOTE: directly pop the ref
            _tracker._subscriber_ref_holder.pop()
            return False, _tracker.meta

    async def test_ongoing_cache_register(self):
        """
        Test multiple access to single resource with ongoing_cache control mechanism.
        """
        coros: list[Coroutine] = []
        for idx in range(self.WORKS_NUM):
            coros.append(self._worker(idx))

        random.shuffle(coros)  # shuffle the corotines to simulate unordered access
        tasks = [asyncio.create_task(c) for c in coros]
        logger.info(f"{self.WORKS_NUM} workers have been dispatched")

        # start all the worker, all the workers will now access the same resouce.
        self.sync_event.set()
        logger.info("all workers start to subscribe to the register")
        await self._wait_for_registeration_finish()  # wait for all workers finish subscribing
        self.writer_done_event.set()  # writer finished

        ###### check the test result ######
        meta_set, writer_meta = set(), None
        for _fut in asyncio.as_completed(tasks):
            is_writer, meta = await _fut
            if meta is None:
                logger.warning(
                    "encount edge condition that subscriber subscribes "
                    "on closed tracker, ignored"
                )
                continue
            meta_set.add(meta)
            if is_writer:
                writer_meta = meta
        # ensure only one meta presented in the set, and it should be
        # the meta from the writer/provider, all the subscriber should use
        # the meta from the writer/provider.
        assert len(meta_set) == 1 and writer_meta in meta_set

        # ensure that the entry in the register is garbage collected
        assert (
            len(self.register._id_ref_dict) == len(self.register._ref_tracker_dict) == 0
        )
