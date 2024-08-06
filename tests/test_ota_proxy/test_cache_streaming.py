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
import random
from pathlib import Path
from typing import Coroutine, Optional

import pytest

from ota_proxy.cache_streaming import CacheTracker, CachingRegister
from ota_proxy.db import CacheMeta

logger = logging.getLogger(__name__)


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
        self.register = CachingRegister()

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

        _tracker = self.register.get_tracker(self.URL)
        # is subscriber
        if _tracker:
            logger.debug(f"#{idx} is subscriber")
            await self.register_finish.acquire()

            while not _tracker.writer_finished:  # simulating cache streaming
                await asyncio.sleep(0.1)
            return False, _tracker.cache_meta

        # is provider
        logger.info(f"#{idx} is provider")

        # NOTE: register the tracker before open the remote fd!
        _tracker = CacheTracker(
            cache_identifier=self.URL,
            base_dir=self.base_dir,
            executor=None,  # type: ignore
            commit_cache_cb=None,  # type: ignore
            below_hard_limit_event=None,  # type: ignore
        )
        self.register.register_tracker(self.URL, _tracker)

        # NOTE: use last_access field to store worker index
        # NOTE 2: bypass provider_start method, directly set tracker property
        cache_meta = CacheMeta(
            last_access=idx,
            url="some_url",
            file_sha256="some_filesha256_value",
        )
        _tracker.cache_meta = cache_meta  # normally it was set by start_provider

        # NOTE: we are not actually start the caching, so not setting
        #   executor, commit_cache_cb and below_hard_limit_event
        await self.register_finish.acquire()

        # manually set the tracker to be started
        _tracker._writer_ready.set()

        # simulate waiting for writer finished downloading
        _tracker.fpath.touch()
        await self.writer_done_event.wait()

        # finished
        _tracker._writer_finished.set()
        logger.info(f"writer #{idx} finished")
        return True, _tracker.cache_meta

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
        assert len(self.register._id_tracker) == 0

        # ensure no tmp files are leftover
        assert len(list(self.base_dir.glob("tmp_*"))) == 0
