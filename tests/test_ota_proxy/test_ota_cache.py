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
import io
import threading
import pytest
import pytest_mock
import random
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from typing import Dict, List, Tuple, Coroutine

from otaclient.ota_proxy.db import CacheMeta
from otaclient.ota_proxy import config as cfg
from otaclient.ota_proxy.ota_cache import LRUCacheHelper, OngoingCachingRegister

logger = logging.getLogger(__name__)


class TestLRUCacheHelper:
    @pytest.fixture(scope="class")
    def prepare_entries(self):
        entries: Dict[str, CacheMeta] = {}
        for target_size, rotate_num in cfg.BUCKET_FILE_SIZE_DICT.items():
            for _i in range(rotate_num):
                url = f"{target_size}#{_i}"
                entries[url] = CacheMeta(
                    url=url,
                    bucket_idx=target_size,
                    size=target_size,
                    sha256hash=str(target_size),
                )

        return entries

    @pytest.fixture(scope="class")
    def launch_lru_helper(self):
        try:
            lru_cache_helper = LRUCacheHelper(":memory:")
            yield lru_cache_helper
        finally:
            lru_cache_helper.close()

    @pytest.fixture(autouse=True)
    def setup_test(self, launch_lru_helper, prepare_entries):
        self.entries: Dict[str, CacheMeta] = prepare_entries
        self.cache_helper: LRUCacheHelper = launch_lru_helper

    def test_commit_entry(self):
        for _, entry in self.entries.items():
            assert self.cache_helper.commit_entry(entry)

    def test_lookup_entry(self):
        target_size, idx = 8 * (1024**2), 6
        target_url = f"{target_size}#{idx}"
        assert (
            self.cache_helper.lookup_entry_by_url(target_url)
            == self.entries[target_url]
        )

    def test_remove_entry(self):
        target_size, idx = 8 * (1024**2), 6
        target_url = f"{target_size}#{idx}"
        assert self.cache_helper.remove_entry_by_url(target_url)

    def test_rotate_cache(self):
        """Ensure the LRUHelper properly rotates the cache entries."""
        # test 1: reserve space for 256 * (1024**2) bucket
        # the later bucket is empty, so we expect this bucket to be cleaned up
        target_bucket = 256 * (1024**2)
        assert (
            entries_to_be_removed := self.cache_helper.rotate_cache(target_bucket)
        ) and len(entries_to_be_removed) == 2

        # test 2: reserve space for 16 * 1024 bucket
        # the next bucket is not empty, so we expecte to remove one entry from the next bucket
        target_bucket, next_bucket = 16 * 1024, 32 * 1024
        expected_hash = str(next_bucket)
        assert (
            entries_to_be_removed := self.cache_helper.rotate_cache(target_bucket)
        ) and entries_to_be_removed[0] == expected_hash


class TestOTAFile:
    TEST_BYTES_LEN = 3 * (1024**2)  # 3MiB
    CHUNK_SIZE = 1024

    @pytest.fixture(autouse=True)
    def setup_test(self, tmp_path: Path):
        self.base_dir = tmp_path / "base_dir"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.test_data = random.getrandbits(self.TEST_BYTES_LEN * 8).to_bytes(
            self.TEST_BYTES_LEN, "little"
        )
        self.test_data_sha256hash = sha256(self.test_data).hexdigest()
        try:
            self._executor = ThreadPoolExecutor()
            self.ongoing_tracker = OngoingCachingRegister(self.base_dir)
            yield
        finally:
            self._executor.shutdown()

    async def _fp_wrapper(self, fp: io.BytesIO):
        while data := fp.read(self.CHUNK_SIZE):
            yield data

    async def test_OTAFile(self, mocker: pytest_mock.MockerFixture):
        from otaclient.ota_proxy.ota_cache import OTAFile

        test_url = "test_url"
        below_hard_limit_event = threading.Event()
        below_hard_limit_event.set()

        ota_file = OTAFile(
            self._fp_wrapper(io.BytesIO(memoryview(self.test_data))),
            CacheMeta(
                url=test_url,
                size=self.TEST_BYTES_LEN,
                sha256hash=self.test_data_sha256hash,
            ),
            below_hard_limit_event=below_hard_limit_event,
            base_dir=self.base_dir,
        )

        # launch a background writter to simulate local cache writting
        register_cache_callback = mocker.MagicMock()
        _tracker, _ = await self.ongoing_tracker.get_tracker(test_url)
        fut = asyncio.get_running_loop().run_in_executor(
            self._executor,
            ota_file.background_write_cache,
            _tracker,
            register_cache_callback,
        )

        # simulating retrieve data from server side
        retrieved_data = io.BytesIO()
        async for _data in ota_file.get_chunks():
            retrieved_data.write(_data)

        await fut  # wait for background cache writing to finish

        ###### check the result ######
        # assert the callback is called
        register_cache_callback.assert_called_once_with(ota_file, _tracker)
        # ensure the cache entry presented
        cached_file = self.base_dir / self.test_data_sha256hash
        assert cached_file.is_file() and cached_file.read_bytes() == self.test_data
        # ensure the retrieved bytes
        assert retrieved_data.getvalue() == self.test_data


class TestOngoingCachingRegister:
    URL = "common_url"
    WORKS_NUM = 128

    @pytest.fixture(autouse=True)
    def setup_test(self, tmp_path: Path):
        self.base_dir = tmp_path / "base_dir"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.register = OngoingCachingRegister(self.base_dir)

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
    ) -> Tuple[bool, CacheMeta]:
        """
        Returns tuple of bool indicates whether the worker is writter, and CacheMeta
        from tracker.
        """
        # simulate multiple works subscribing the register
        await self.sync_event.wait()
        await asyncio.sleep(random.randrange(100, 200) // 100)
        _tracker, _is_writer = await self.register.get_tracker(self.URL)
        await self.register_finish.acquire()
        if _is_writer:
            logger.info(f"#{idx} is provider")
            # NOTE: use last_access field to store worker index
            await _tracker.writer_on_ready(CacheMeta(last_access=idx))
            # simulate waiting for writer finished downloading
            await self.writer_done_event.wait()
            _tracker.writer_on_finish()
            return True, _tracker.meta
        else:
            logger.debug(f"#{idx} is subscriber")
            # wait for writter become ready
            await _tracker.writer_ready.wait()
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
            meta_set.add(meta)
            if is_writer:
                writer_meta = meta
        # ensure only one meta presented in the set, and it should be
        # the meta from the writer/provider, all the subscriber should use
        # the meta from the writer/provider.
        assert len(meta_set) == 1 and writer_meta in meta_set

        # ensure that the entry in the register is garbage collected
        assert (
            len(self.register._url_ref_dict)
            == len(self.register._ref_tracker_dict)
            == 0
        )
