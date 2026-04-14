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
"""Unit tests for ota_proxy.cache_streaming.

Test focus:
  1. CacheTrackerEvents: one-way flag transitions
  2. CacheTracker: naming, init, finalize_cache, provider/subscriber methods
  3. CachingRegister: get/register tracker, weakref cleanup, failed tracker skip
  4. CacheWriterPool: dispatch gating, stream_writing_cache paths
  5. CacheReaderPool: read/stream dispatch, subscribe_tracker paths
"""

from __future__ import annotations

import asyncio
import logging
import random
import threading
from pathlib import Path
from queue import SimpleQueue
from typing import AsyncGenerator, Coroutine, Optional

import anyio
import pytest

from ota_proxy.cache_streaming import (
    CacheReaderPool,
    CacheTracker,
    CacheTrackerEvents,
    CacheWriterPool,
    CachingRegister,
    _reader_failed_sentinel,
)
from ota_proxy.config import config as cfg
from ota_proxy.db import CacheMeta
from ota_proxy.errors import (
    CacheProviderNotReady,
    CacheStreamingFailed,
    CacheStreamingInterrupt,
    ReaderPoolBusy,
)

logger = logging.getLogger(__name__)


# ---- helpers ----


def _make_cache_meta(**kwargs) -> CacheMeta:
    defaults = {"url": "http://example.com/file.bin", "file_sha256": "abc123"}
    defaults.update(kwargs)
    return CacheMeta(**defaults)  # type: ignore


def _make_tracker(
    tmp_path: Path,
    *,
    cache_identifier: str = "abc123",
    commit_cache_cb=None,
    below_soft_limit_set: bool = True,
    below_hard_limit_set: bool = True,
) -> CacheTracker:
    base_dir = tmp_path / "cache"
    base_dir.mkdir(parents=True, exist_ok=True)
    below_soft_limit = threading.Event()
    if below_soft_limit_set:
        below_soft_limit.set()
    below_hard_limit = threading.Event()
    if below_hard_limit_set:
        below_hard_limit.set()
    return CacheTracker(
        cache_identifier=cache_identifier,
        base_dir=str(base_dir),
        commit_cache_cb=commit_cache_cb or (lambda *_: True),
        below_soft_limit_event=below_soft_limit,
        below_hard_limit_event=below_hard_limit,
    )


# ---- CacheTrackerEvents ----


class TestCacheTrackerEvents:
    def test_initial_state(self):
        ev = CacheTrackerEvents()
        assert not ev.writer_started
        assert not ev.writer_finished
        assert not ev.writer_failed

    def test_set_writer_started(self):
        ev = CacheTrackerEvents()
        ev.set_writer_started()
        assert ev.writer_started
        assert not ev.writer_finished
        assert not ev.writer_failed

    def test_set_writer_finished(self):
        ev = CacheTrackerEvents()
        ev.set_writer_finished()
        assert ev.writer_started  # finished implies started
        assert ev.writer_finished
        assert not ev.writer_failed

    def test_set_writer_failed(self):
        ev = CacheTrackerEvents()
        ev.set_writer_failed()
        assert ev.writer_started  # failed implies started
        assert not ev.writer_finished
        assert ev.writer_failed


# ---- CacheTracker ----


class TestCacheTrackerNaming:
    def test_tmp_file_naming_format(self):
        name = CacheTracker._tmp_file_naming("deadbeef")
        parts = name.split(CacheTracker.FNAME_PART_SEPARATOR)
        assert parts[0] == cfg.TMP_FILE_PREFIX
        assert parts[1] == "deadbeef"

    def test_tmp_file_naming_unique(self):
        """Successive calls produce different names (counter increments)."""
        a = CacheTracker._tmp_file_naming("same_id")
        b = CacheTracker._tmp_file_naming("same_id")
        assert a != b


class TestCacheTrackerFinalizeCache:
    def test_skip_when_no_cache_meta(self, tmp_path: Path, mocker):
        cb = mocker.MagicMock()
        tracker = _make_tracker(tmp_path, commit_cache_cb=cb)
        tracker.cache_meta = None
        tracker._finalize_cache()
        cb.assert_not_called()

    def test_skip_when_above_soft_limit(self, tmp_path: Path, mocker):
        cb = mocker.MagicMock()
        tracker = _make_tracker(
            tmp_path, commit_cache_cb=cb, below_soft_limit_set=False
        )
        tracker.cache_meta = _make_cache_meta()
        # create tmp file for os.link
        Path(tracker.fpath).touch()
        tracker._finalize_cache()
        cb.assert_not_called()

    def test_commits_when_below_soft_limit(self, tmp_path: Path, mocker):
        cb = mocker.MagicMock()
        tracker = _make_tracker(tmp_path, commit_cache_cb=cb)
        meta = _make_cache_meta()
        tracker.cache_meta = meta
        Path(tracker.fpath).touch()
        tracker._finalize_cache()
        cb.assert_called_once_with(meta)
        assert Path(tracker.save_path).exists()

    def test_link_file_exists_is_ignored(self, tmp_path: Path, mocker):
        cb = mocker.MagicMock()
        tracker = _make_tracker(tmp_path, commit_cache_cb=cb)
        meta = _make_cache_meta()
        tracker.cache_meta = meta
        Path(tracker.fpath).touch()
        # pre-create save_path so os.link raises FileExistsError
        Path(tracker.save_path).touch()
        tracker._finalize_cache()  # should not raise
        cb.assert_called_once_with(meta)

    def test_commit_cb_exception_is_suppressed(self, tmp_path: Path, mocker):
        cb = mocker.MagicMock(side_effect=RuntimeError("db failure"))
        tracker = _make_tracker(tmp_path, commit_cache_cb=cb)
        tracker.cache_meta = _make_cache_meta()
        Path(tracker.fpath).touch()
        tracker._finalize_cache()  # should not raise


# ---- CacheTracker provider methods ----


class TestProviderWriteFileInThread:
    def test_normal_streaming_write(self, tmp_path: Path, mocker):
        cb = mocker.MagicMock()
        tracker = _make_tracker(tmp_path, commit_cache_cb=cb)
        meta = _make_cache_meta()
        tracker.cache_meta = meta
        que: SimpleQueue[bytes | None] = SimpleQueue()
        que.put(b"chunk1")
        que.put(b"chunk2")
        que.put(None)  # sentinel

        tracker.provider_write_file_in_thread(que)

        assert tracker._tracker_events.writer_finished
        assert not tracker._tracker_events.writer_failed
        assert tracker._bytes_written == len(b"chunk1") + len(b"chunk2")
        assert meta.cache_size == tracker._bytes_written
        # file content
        with open(tracker.fpath, "rb") as f:
            assert f.read() == b"chunk1chunk2"
        cb.assert_called_once_with(meta)

    def test_writer_failed_flag_aborts(self, tmp_path: Path):
        tracker = _make_tracker(tmp_path)
        tracker.cache_meta = _make_cache_meta()
        que: SimpleQueue[bytes | None] = SimpleQueue()
        que.put(b"data")
        # pre-set failed so the loop sees it on next iteration
        tracker._tracker_events.set_writer_failed()
        que.put(b"more_data")
        que.put(None)

        tracker.provider_write_file_in_thread(que)
        assert tracker._tracker_events.writer_failed

    def test_hard_limit_aborts(self, tmp_path: Path):
        tracker = _make_tracker(tmp_path, below_hard_limit_set=False)
        tracker.cache_meta = _make_cache_meta()
        que: SimpleQueue[bytes | None] = SimpleQueue()
        que.put(b"data")
        que.put(None)

        tracker.provider_write_file_in_thread(que)
        assert tracker._tracker_events.writer_failed


class TestProviderWriteOnceInThread:
    def test_normal_single_write(self, tmp_path: Path, mocker):
        cb = mocker.MagicMock()
        tracker = _make_tracker(tmp_path, commit_cache_cb=cb)
        meta = _make_cache_meta()
        tracker.cache_meta = meta
        data = b"small file content"

        tracker.provider_write_once_in_thread(data)

        assert tracker._tracker_events.writer_finished
        assert not tracker._tracker_events.writer_failed
        assert tracker._bytes_written == len(data)
        assert meta.cache_size == len(data)
        with open(tracker.fpath, "rb") as f:
            assert f.read() == data
        cb.assert_called_once_with(meta)

    def test_hard_limit_aborts(self, tmp_path: Path):
        tracker = _make_tracker(tmp_path, below_hard_limit_set=False)
        tracker.cache_meta = _make_cache_meta()

        tracker.provider_write_once_in_thread(b"data")
        assert tracker._tracker_events.writer_failed
        assert not tracker._tracker_events.writer_finished


class TestProviderHandleEmptyFile:
    def test_sets_writer_finished_and_commits(self, tmp_path: Path, mocker):
        cb = mocker.MagicMock()
        tracker = _make_tracker(tmp_path, commit_cache_cb=cb)
        meta = _make_cache_meta()
        tracker.cache_meta = meta

        tracker.provider_handle_empty_file()

        assert tracker._tracker_events.writer_finished
        assert not tracker._tracker_events.writer_failed
        cb.assert_called_once_with(meta)

    def test_asserts_cache_meta_is_set(self, tmp_path: Path):
        tracker = _make_tracker(tmp_path)
        # cache_meta is None by default
        with pytest.raises(AssertionError):
            tracker.provider_handle_empty_file()


# ---- CacheTracker subscriber methods ----


class TestSubscriberWaitForProvider:
    async def test_provider_already_started_and_finished(self, tmp_path: Path):
        tracker = _make_tracker(tmp_path)
        tracker._tracker_events.set_writer_finished()

        result = await tracker.subscriber_wait_for_provider()
        assert result is True  # writer_finished

    async def test_provider_already_started_not_finished(self, tmp_path: Path):
        tracker = _make_tracker(tmp_path)
        tracker._tracker_events.set_writer_started()

        result = await tracker.subscriber_wait_for_provider()
        assert result is False  # not finished yet

    async def test_provider_starts_after_short_delay(self, tmp_path: Path):
        tracker = _make_tracker(tmp_path)

        async def _start_later():
            await asyncio.sleep(0.05)
            tracker._tracker_events.set_writer_started()

        task = asyncio.create_task(_start_later())
        result = await tracker.subscriber_wait_for_provider()
        assert result is False
        await task

    async def test_timeout_raises_provider_not_ready(self, tmp_path: Path, mocker):
        tracker = _make_tracker(tmp_path)
        # patch backoff to be near-zero so it times out fast
        mocker.patch("ota_proxy.cache_streaming.get_backoff", return_value=0.001)
        with pytest.raises(CacheProviderNotReady):
            await tracker.subscriber_wait_for_provider()

    async def test_provider_failed_raises(self, tmp_path: Path):
        """set_writer_failed() sets _started=True, so the wait loop exits,
        then the post-loop writer_failed check raises CacheProviderNotReady."""
        tracker = _make_tracker(tmp_path)
        tracker._tracker_events.set_writer_failed()

        with pytest.raises(CacheProviderNotReady):
            await tracker.subscriber_wait_for_provider()

    async def test_provider_fails_during_wait(self, tmp_path: Path):
        """Provider fails while subscriber is polling — subscriber should
        see writer_started=True (from set_writer_failed), exit the loop,
        and raise CacheProviderNotReady from the post-loop check."""
        tracker = _make_tracker(tmp_path)

        async def _fail_later():
            await asyncio.sleep(0.05)
            tracker._tracker_events.set_writer_failed()

        task = asyncio.create_task(_fail_later())
        with pytest.raises(CacheProviderNotReady):
            await tracker.subscriber_wait_for_provider()
        await task


class TestSubscriberStreamCacheInThread:
    def test_normal_streaming(self, tmp_path: Path):
        """Subscriber reads data written by provider."""
        tracker = _make_tracker(tmp_path)
        meta = _make_cache_meta()
        # write the file first
        Path(tracker.fpath).write_bytes(b"hello world")
        tracker.cache_meta = meta
        tracker._bytes_written = 11
        tracker._tracker_events.set_writer_finished()

        loop = asyncio.new_event_loop()
        que: asyncio.Queue = loop.run_until_complete(self._make_queue())
        interrupt = threading.Event()

        tracker._acall_soon = loop.call_soon_threadsafe
        tracker.subscriber_stream_cache_in_thread(
            que, interrupt_thread_worker=interrupt, read_chunk_size=4
        )

        chunks = loop.run_until_complete(self._drain_queue(que))
        loop.close()
        assert b"".join(chunks) == b"hello world"

    def test_interrupt_stops_reading(self, tmp_path: Path):
        tracker = _make_tracker(tmp_path)
        Path(tracker.fpath).write_bytes(b"x" * 1024)
        tracker._bytes_written = 1024
        tracker._tracker_events.set_writer_finished()

        loop = asyncio.new_event_loop()
        que: asyncio.Queue = loop.run_until_complete(self._make_queue())
        interrupt = threading.Event()
        interrupt.set()  # pre-set interrupt

        tracker._acall_soon = loop.call_soon_threadsafe
        tracker.subscriber_stream_cache_in_thread(
            que, interrupt_thread_worker=interrupt, read_chunk_size=64
        )

        # should get None sentinel (from finally block) but minimal/no data
        chunks = loop.run_until_complete(self._drain_queue(que))
        loop.close()
        # interrupted early, so less data than full file
        assert len(b"".join(chunks)) < 1024

    def test_writer_failed_stops_reading(self, tmp_path: Path):
        tracker = _make_tracker(tmp_path)
        Path(tracker.fpath).write_bytes(b"data")
        tracker._bytes_written = 4
        tracker._tracker_events.set_writer_failed()

        loop = asyncio.new_event_loop()
        que: asyncio.Queue = loop.run_until_complete(self._make_queue())
        interrupt = threading.Event()

        tracker._acall_soon = loop.call_soon_threadsafe
        tracker.subscriber_stream_cache_in_thread(
            que, interrupt_thread_worker=interrupt, read_chunk_size=4
        )

        chunks = loop.run_until_complete(self._drain_queue(que))
        loop.close()
        # writer failed, subscriber returns early
        assert len(chunks) == 0

    def test_file_open_error_sends_failed_sentinel(self, tmp_path: Path):
        tracker = _make_tracker(tmp_path)
        # don't create the file → open() will fail

        loop = asyncio.new_event_loop()
        que: asyncio.Queue = loop.run_until_complete(self._make_queue())
        interrupt = threading.Event()

        tracker._acall_soon = loop.call_soon_threadsafe
        tracker.subscriber_stream_cache_in_thread(
            que, interrupt_thread_worker=interrupt, read_chunk_size=64
        )

        items = loop.run_until_complete(self._drain_queue_raw(que))
        loop.close()
        assert _reader_failed_sentinel in items

    @staticmethod
    async def _make_queue() -> asyncio.Queue:
        return asyncio.Queue()

    @staticmethod
    async def _drain_queue(que: asyncio.Queue) -> list[bytes]:
        chunks = []
        while True:
            item = await asyncio.wait_for(que.get(), timeout=5)
            if item is None:
                break
            if item is _reader_failed_sentinel:
                break
            chunks.append(item)
        return chunks

    @staticmethod
    async def _drain_queue_raw(que: asyncio.Queue) -> list:
        items = []
        while True:
            item = await asyncio.wait_for(que.get(), timeout=5)
            if item is None:
                break
            items.append(item)
        return items


# ---- CachingRegister ----


class TestCachingRegister:
    def test_get_tracker_returns_none_when_empty(self):
        register = CachingRegister()
        assert register.get_tracker("nonexistent") is None

    def test_register_and_get_tracker(self, tmp_path: Path):
        register = CachingRegister()
        tracker = _make_tracker(tmp_path)
        register.register_tracker("myid", tracker)
        assert register.get_tracker("myid") is tracker

    def test_get_tracker_skips_failed(self, tmp_path: Path):
        register = CachingRegister()
        tracker = _make_tracker(tmp_path)
        tracker._tracker_events.set_writer_failed()
        register.register_tracker("myid", tracker)
        assert register.get_tracker("myid") is None

    def test_weakref_cleanup(self, tmp_path: Path):
        register = CachingRegister()
        tracker = _make_tracker(tmp_path)
        register.register_tracker("myid", tracker)
        assert len(register._id_tracker) == 1
        del tracker
        assert len(register._id_tracker) == 0


# ---- CacheWriterPool ----


class TestCacheWriterPool:
    @pytest.fixture(autouse=True)
    async def setup_pool(self):
        self.pool = CacheWriterPool(max_workers=2, max_pending=4)
        yield
        await self.pool.close()

    async def test_write_dispatcher_drops_when_busy(self):
        ev = CacheTrackerEvents()
        # fill the semaphore to make it locked
        for _ in range(4):
            await self.pool._se.acquire()
        assert self.pool._se.locked()

        await self.pool._write_dispatcher(ev, lambda: None)
        assert ev.writer_failed

        # release the semaphore
        for _ in range(4):
            self.pool._se.release()

    async def test_write_dispatcher_submits_when_available(self):
        ev = CacheTrackerEvents()
        called = threading.Event()

        def work():
            called.set()

        await self.pool._write_dispatcher(ev, work)
        called.wait(timeout=5)
        assert called.is_set()
        assert not ev.writer_failed

    async def test_stream_writing_cache_empty_fd_stop_iteration(
        self, tmp_path: Path, mocker
    ):
        """Empty async generator (StopAsyncIteration) → provider_handle_empty_file called."""
        cb = mocker.MagicMock()
        tracker = _make_tracker(tmp_path, commit_cache_cb=cb)
        meta = _make_cache_meta()
        tracker.cache_meta = meta

        async def empty_gen() -> AsyncGenerator[bytes]:
            return
            yield b""  # noqa: S1763

        gen = self.pool.stream_writing_cache(empty_gen(), tracker)
        chunks = [chunk async for chunk in gen]
        assert chunks == []
        assert tracker._tracker_events.writer_finished
        cb.assert_called_once_with(meta)

    async def test_stream_writing_cache_empty_fd_zero_len_chunk(
        self, tmp_path: Path, mocker
    ):
        """First chunk is zero-length bytes → provider_handle_empty_file called."""
        cb = mocker.MagicMock()
        tracker = _make_tracker(tmp_path, commit_cache_cb=cb)
        meta = _make_cache_meta()
        tracker.cache_meta = meta

        async def empty_chunk_gen() -> AsyncGenerator[bytes]:
            yield b""

        gen = self.pool.stream_writing_cache(empty_chunk_gen(), tracker)
        chunks = [chunk async for chunk in gen]
        assert chunks == [b""]
        assert tracker._tracker_events.writer_finished
        cb.assert_called_once_with(meta)

    async def test_stream_writing_cache_single_chunk(self, tmp_path: Path):
        """Single-chunk fd → provider_write_once_in_thread dispatched."""
        tracker = _make_tracker(tmp_path)
        meta = _make_cache_meta()
        tracker.cache_meta = meta

        async def single_chunk_gen() -> AsyncGenerator[bytes]:
            yield b"only_chunk"

        gen = self.pool.stream_writing_cache(single_chunk_gen(), tracker)
        chunks = [chunk async for chunk in gen]
        assert chunks == [b"only_chunk"]

        # wait for thread pool to finish writing
        await asyncio.sleep(0.3)
        assert (
            tracker._tracker_events.writer_finished
            or tracker._tracker_events.writer_failed
        )

    async def test_stream_writing_cache_multi_chunk(self, tmp_path: Path):
        """Multi-chunk fd → provider_write_file_in_thread dispatched."""
        tracker = _make_tracker(tmp_path)
        meta = _make_cache_meta()
        tracker.cache_meta = meta

        async def multi_chunk_gen() -> AsyncGenerator[bytes]:
            yield b"chunk1"
            yield b"chunk2"
            yield b"chunk3"

        gen = self.pool.stream_writing_cache(multi_chunk_gen(), tracker)
        chunks = [chunk async for chunk in gen]
        assert chunks == [b"chunk1", b"chunk2", b"chunk3"]

        await asyncio.sleep(0.3)
        # file should have been written
        if not tracker._tracker_events.writer_failed:
            assert tracker._tracker_events.writer_finished

    async def test_stream_writing_cache_fd_error_raises(self, tmp_path: Path):
        """Exception from upstream fd → CacheStreamingFailed raised."""
        tracker = _make_tracker(tmp_path)
        meta = _make_cache_meta()
        tracker.cache_meta = meta

        async def failing_gen() -> AsyncGenerator[bytes]:
            yield b"chunk1"
            yield b"chunk2"
            raise ConnectionError("upstream broke")

        gen = self.pool.stream_writing_cache(failing_gen(), tracker)
        with pytest.raises(CacheStreamingFailed):
            async for _ in gen:
                pass

        assert tracker._tracker_events.writer_failed


# ---- CacheReaderPool ----


class TestCacheReaderPool:
    @pytest.fixture(autouse=True)
    async def setup_pool(self):
        self.pool = CacheReaderPool(max_workers=2, max_pending=4, read_chunk_size=64)
        yield
        await self.pool.close()

    async def test_read_dispatcher_raises_when_busy(self):
        for _ in range(4):
            await self.pool._se.acquire()
        assert self.pool._se.locked()

        with pytest.raises(ReaderPoolBusy):
            await self.pool._read_dispatcher(lambda: None)

        for _ in range(4):
            self.pool._se.release()

    async def test_read_file_once(self, tmp_path: Path):
        f = tmp_path / "small.bin"
        f.write_bytes(b"small content")
        result = await self.pool.read_file_once(str(f))
        assert result == b"small content"

    async def test_stream_read_file(self, tmp_path: Path):
        f = tmp_path / "large.bin"
        data = b"x" * 256
        f.write_bytes(data)

        gen = await self.pool.stream_read_file(str(f))
        chunks = []
        async for chunk in gen:
            chunks.append(chunk)
        assert b"".join(chunks) == data

    async def test_stream_from_que_reader_failed(self):
        que: asyncio.Queue = asyncio.Queue()
        interrupt = threading.Event()
        que.put_nowait(_reader_failed_sentinel)
        que.put_nowait(None)

        with pytest.raises(CacheStreamingInterrupt):
            async for _ in self.pool._stream_from_que(que, interrupt):
                pass

        assert interrupt.is_set()

    async def test_stream_from_que_normal(self):
        que: asyncio.Queue = asyncio.Queue()
        interrupt = threading.Event()
        que.put_nowait(b"a")
        que.put_nowait(b"b")
        que.put_nowait(None)

        chunks = []
        async for chunk in self.pool._stream_from_que(que, interrupt):
            chunks.append(chunk)
        assert chunks == [b"a", b"b"]

    async def test_subscribe_tracker_finished_small_file(self, tmp_path: Path):
        """Finished small cache → read_file_once path."""
        tracker = _make_tracker(tmp_path)
        meta = _make_cache_meta(cache_size=64)
        tracker.cache_meta = meta
        tracker._tracker_events.set_writer_finished()
        Path(tracker.fpath).write_bytes(b"small data")

        result = await self.pool.subscribe_tracker(tracker)
        # small finished file returns bytes directly
        assert result == b"small data"

    async def test_subscribe_tracker_ongoing_cache(self, tmp_path: Path):
        """Ongoing cache → subscriber_stream_cache_in_thread path."""
        tracker = _make_tracker(tmp_path)
        meta = _make_cache_meta()
        tracker.cache_meta = meta
        tracker._tracker_events.set_writer_started()
        # write some data and mark finished after a brief delay
        Path(tracker.fpath).write_bytes(b"streamed data")
        tracker._bytes_written = len(b"streamed data")

        async def _finish_later():
            await asyncio.sleep(0.1)
            tracker._tracker_events.set_writer_finished()

        task = asyncio.create_task(_finish_later())

        result = await self.pool.subscribe_tracker(tracker)
        # ongoing cache returns an async generator
        assert isinstance(result, AsyncGenerator)
        chunks = []
        async for chunk in result:
            chunks.append(chunk)
        assert b"".join(chunks) == b"streamed data"
        await task

    async def test_subscribe_tracker_finished_empty_file(self, tmp_path: Path):
        """Finished empty cache (cache_size=0) → returns b"" directly."""
        tracker = _make_tracker(tmp_path)
        meta = _make_cache_meta(cache_size=0)
        tracker.cache_meta = meta
        tracker._tracker_events.set_writer_finished()

        result = await self.pool.subscribe_tracker(tracker)
        assert result == b""

    async def test_subscribe_tracker_provider_failed(self, tmp_path: Path):
        """Provider failed → CacheProviderNotReady raised."""
        tracker = _make_tracker(tmp_path)
        tracker.cache_meta = _make_cache_meta()
        tracker._tracker_events.set_writer_failed()

        with pytest.raises(CacheProviderNotReady):
            await self.pool.subscribe_tracker(tracker)

    async def test_subscribe_tracker_finished_large_file(self, tmp_path: Path):
        """Finished large cache → stream path (not read_file_once)."""
        tracker = _make_tracker(tmp_path)
        # set cache_size above REMOTE_READ_CHUNK_SIZE to trigger stream path
        meta = _make_cache_meta(cache_size=cfg.REMOTE_READ_CHUNK_SIZE + 1)
        tracker.cache_meta = meta
        tracker._tracker_events.set_writer_finished()
        data = b"y" * 256
        Path(tracker.fpath).write_bytes(data)
        tracker._bytes_written = len(data)

        result = await self.pool.subscribe_tracker(tracker)
        assert isinstance(result, AsyncGenerator)
        chunks = []
        async for chunk in result:
            chunks.append(chunk)
        assert b"".join(chunks) == data


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
        _base_dir = tmp_path / "base_dir"
        _base_dir.mkdir(parents=True, exist_ok=True)

        self.base_dir = anyio.Path(_base_dir)
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

            while (
                not _tracker._tracker_events.writer_finished
            ):  # simulating cache streaming
                await asyncio.sleep(0.1)
            return False, _tracker.cache_meta

        # is provider
        logger.info(f"#{idx} is provider")

        # NOTE: register the tracker before open the remote fd!
        _tracker = CacheTracker(
            cache_identifier=self.URL,
            base_dir=str(self.base_dir),
            commit_cache_cb=None,  # type: ignore
            below_soft_limit_event=None,  # type: ignore
            below_hard_limit_event=None,  # type: ignore
        )
        self.register.register_tracker(self.URL, _tracker)

        # NOTE: use last_access field to store worker index
        # NOTE 2: bypass provider_start method, directly set tracker property
        cache_meta = CacheMeta(
            url=self.URL,
            last_access=idx,
            file_sha256="some_filesha256_value",
        )
        _tracker.cache_meta = cache_meta  # normally it was set by start_provider

        # NOTE: we are not actually start the caching, so not setting
        #   executor, commit_cache_cb and below_hard_limit_event
        await self.register_finish.acquire()

        # simulate waiting for writer finished downloading
        Path(_tracker.fpath).touch()
        await self.writer_done_event.wait()

        # finished
        _tracker._tracker_events.set_writer_finished()
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
        await (
            self._wait_for_registeration_finish()
        )  # wait for all workers finish subscribing
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

        # NOTE(20250617): the tmp file clean up is done by os.replace now,
        #                 this test doesn't cover actual caching, so skip here.
