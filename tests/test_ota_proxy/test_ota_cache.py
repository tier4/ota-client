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


import asyncio
import logging
import random
from pathlib import Path
from typing import Coroutine, Dict, List, Optional, Tuple

import pytest

from ota_proxy import config as cfg
from ota_proxy.db import CacheMeta, OTACacheDB
from ota_proxy.ota_cache import CachingRegister, LRUCacheHelper
from ota_proxy.utils import url_based_hash

logger = logging.getLogger(__name__)


class TestLRUCacheHelper:
    @pytest.fixture(scope="class")
    def prepare_entries(self):
        entries: Dict[str, CacheMeta] = {}
        for target_size, rotate_num in cfg.BUCKET_FILE_SIZE_DICT.items():
            for _i in range(rotate_num):
                mocked_url = f"{target_size}#{_i}"
                entries[mocked_url] = CacheMeta(
                    url=mocked_url,
                    bucket_idx=target_size,
                    cache_size=target_size,
                    file_sha256=url_based_hash(mocked_url),
                )

        return entries

    @pytest.fixture(scope="class")
    def launch_lru_helper(self, tmp_path_factory: pytest.TempPathFactory):
        # init db
        ota_cache_folder = tmp_path_factory.mktemp("ota-cache")
        self._db_f = ota_cache_folder / "db_f"
        OTACacheDB.init_db_file(self._db_f)

        lru_cache_helper = LRUCacheHelper(self._db_f)
        try:
            yield lru_cache_helper
        finally:
            lru_cache_helper.close()

    @pytest.fixture(autouse=True)
    def setup_test(self, launch_lru_helper, prepare_entries):
        self.entries: Dict[str, CacheMeta] = prepare_entries
        self.cache_helper: LRUCacheHelper = launch_lru_helper

    async def test_commit_entry(self):
        for _, entry in self.entries.items():
            assert await self.cache_helper.commit_entry(entry)

    async def test_lookup_entry(self):
        target_size, idx = 8 * (1024**2), 6
        target_url = f"{target_size}#{idx}"
        file_sha256 = url_based_hash(target_url)

        assert (
            await self.cache_helper.lookup_entry(file_sha256)
            == self.entries[target_url]
        )

    async def test_remove_entry(self):
        target_size, idx = 8 * (1024**2), 6
        target_file_sha256 = url_based_hash(f"{target_size}#{idx}")
        assert await self.cache_helper.remove_entry(target_file_sha256)

    async def test_rotate_cache(self):
        """Ensure the LRUHelper properly rotates the cache entries."""
        # test 1: reserve space for 32 * (1024**2) bucket
        #   the 32MB bucket is the last bucket and will not be rotated.
        target_bucket = 32 * (1024**2)
        entries_to_be_removed = await self.cache_helper.rotate_cache(target_bucket)
        assert entries_to_be_removed is not None and len(entries_to_be_removed) == 0

        # test 2: reserve space for 8 * 1024 bucket
        # the next bucket is not empty, so we expecte to remove one entry from the next bucket
        target_bucket = 8 * 1024
        assert (
            entries_to_be_removed := await self.cache_helper.rotate_cache(target_bucket)
        ) and len(entries_to_be_removed) == 1


class TestOngoingCachingRegister:
    """
    NOTE; currently this test only testing the weakref implementation part,
          the file descriptor management part is tested in test_ota_proxy_server
    """

    URL = "common_url"
    WORKS_NUM = 128

    @pytest.fixture(autouse=True)
    def setup_test(self, tmp_path: Path):
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
    ) -> Tuple[bool, Optional[CacheMeta]]:
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
            _tracker.meta = CacheMeta(last_access=idx)
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

    async def test_OngoingCachingRegister(self):
        coros: List[Coroutine] = []
        for idx in range(self.WORKS_NUM):
            coros.append(self._worker(idx))
        random.shuffle(coros)  # shuffle the corotines to simulate unordered access
        tasks = [asyncio.create_task(c) for c in coros]
        logger.info(f"{self.WORKS_NUM} workers have been dispatched")

        # start all the worker
        self.sync_event.set()
        logger.info("all workers start to subscribe to the register")
        await self._wait_for_registeration_finish()  # wait for all workers finish subscribing
        self.writer_done_event.set()  # writer finished

        ###### check the test result ######
        meta_set, writer_meta = set(), None
        for is_writer, meta in await asyncio.gather(*tasks):
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
