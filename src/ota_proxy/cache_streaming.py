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
"""Implementation of cache streaming."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from typing import Any, AsyncGenerator, Callable

import anyio
from anyio.to_thread import run_sync

from otaclient_common._logging import get_burst_suppressed_logger
from otaclient_common._typing import StrOrPath
from otaclient_common.common import get_backoff

from .config import config as cfg
from .db import CacheMeta
from .errors import (
    CacheStreamingFailed,
    CacheStreamingInterrupt,
)

logger = logging.getLogger(__name__)
# NOTE: for request_error, only allow max 6 lines of logging per 30 seconds
burst_suppressed_logger = get_burst_suppressed_logger(f"{__name__}.handle_error")

# In case of all the reader threads are busy,
#   not let the caller waits for too long.
WAIT_READ_TIMEOUT = 8  # seconds

# In case of all the writer threads are busy,
#   not piling up pending data for too long.
WAIT_WRITE_TIMEOUT = 16  # seconds

_reader_failed_sentinel = object()
_writer_failed_sentinel = object()

# cache tracker


def _unlink_no_error(fpath):
    try:
        os.unlink(fpath)
    except Exception:
        pass


class CacheTracker:
    """A tracker for an ongoing cache entry.

    This tracker represents an ongoing cache entry under the <cache_dir>,
    and takes care of the life cycle of this temp cache entry.
    It implements the provider/subscriber model for cache writing and cache streaming to
    multiple clients.

    This entry will disappear automatically, ensured by gc and weakref
    when no strong reference to this tracker(provider finished and no subscribers
    attached to this tracker).
    When this tracker is garbage collected, the corresponding temp cache entry will
    also be removed automatically(ensure by registered finalizer).

    Attributes:
        fpath: the path to the temporary cache file.
        save_path: the path to finished cache file.
        meta: an inst of CacheMeta for the remote OTA file that being cached.
        writer_ready: a property indicates whether the provider is
            ready to write and streaming data chunks.
        writer_finished: a property indicates whether the provider finished the caching.
        writer_failed: a property indicates whether provider fails to
            finish the caching.
    """

    SUBSCRIBER_WAIT_PROVIDER_READY_MAX_RETRY = 4  # max wait: ~1s
    SUBSCRIBER_WAIT_NEXT_CHUNK_MAX_RETRY = 4  # max wait: ~1s
    SUBSCRIBER_WAIT_BACKOFF_FACTOR = 0.01
    SUBSCRIBER_WAIT_BACKOFF_MAX = 1
    FNAME_PART_SEPARATOR = "_"

    @classmethod
    def _tmp_file_naming(cls, cache_identifier: str) -> str:
        """Create fname for tmp caching entry.

        naming scheme: tmp_<file_sha256>_<hex_of_4bytes_random>

        NOTE: append 4bytes hex to identify cache entry for the same OTA file between
              different trackers.
        """
        return (
            f"{cfg.TMP_FILE_PREFIX}{cls.FNAME_PART_SEPARATOR}"
            f"{cache_identifier}{cls.FNAME_PART_SEPARATOR}{(os.urandom(4)).hex()}"
        )

    def __init__(
        self,
        cache_identifier: str,
        *,
        base_dir: anyio.Path,
        commit_cache_cb: _CACHE_ENTRY_REGISTER_CALLBACK,
        below_hard_limit_event: threading.Event,
    ):
        self.fpath = base_dir / self._tmp_file_naming(cache_identifier)
        self.save_path = base_dir / cache_identifier
        self.cache_meta: CacheMeta | None = None
        self._commit_cache_cb = commit_cache_cb

        self._writer_finished = asyncio.Event()
        self._writer_failed = threading.Event()

        self._space_availability_event = below_hard_limit_event

        self._bytes_written = 0
        self._loop = asyncio.get_event_loop()
        self._acall_soon = self._loop.call_soon_threadsafe

    @property
    def writer_failed(self) -> bool:
        return self._writer_failed.is_set()

    def set_writer_failed(self) -> None:  # pragma: no cover
        self._writer_finished.set()
        self._writer_failed.set()

    # exposed API

    def provider_write_file_in_thread(
        self, cache_meta: CacheMeta, input_que: Queue[bytes | None]
    ) -> None:
        try:
            with open(self.fpath, "wb") as f:
                fd = f.fileno()
                # caller set failed flag, abort
                if self.writer_failed:
                    return

                # first create the file
                f.write(b"")
                f.flush()
                os.fsync(fd)

                self.cache_meta = cache_meta
                weakref.finalize(self, _unlink_no_error, self.fpath)

                try:
                    while data := input_que.get():
                        if not self._space_availability_event.is_set():
                            return
                        f.write(data)
                finally:
                    f.flush()
                    os.fsync(fd)
                    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)

            # NOTE(20240805): mark the writer succeeded in advance to release the
            #   subscriber faster. Whether the database entry is committed or not
            #   doesn't matter here, the subscriber doesn't need to fail if caching
            #   finished but db commit failed.
            self._writer_finished.set()
            self.cache_meta.cache_size = self._bytes_written

            if not self.writer_failed:
                os.link(self.fpath, self.save_path)
                self._commit_cache_cb(self.cache_meta)
        except Exception as e:
            self.set_writer_failed()
            burst_suppressed_logger.exception(f"cache writing failed: {e!r}")
        finally:
            del self, input_que

    def subscriber_stream_cache_in_thread(
        self,
        output_que: asyncio.Queue[bytes | None | Any],
        *,
        reader_ready_event: asyncio.Event,
        interrupt_thread_worker: threading.Event,
        thread_local,
    ) -> None:
        """Subscriber keeps polling chunks from ongoing tmp cache file.

        Subscriber will keep polling until the provider fails or
        provider finished and subscriber has read <bytes_written> bytes.
        """
        # first we wait for provider starts
        _wait_count = 0
        # NOTE: when cache_meta is bound to the tracker, the provider has
        #       already create the cache file, see provider_write_file_in_thread
        #       for more details.
        while self.cache_meta is None:
            if (
                _wait_count > self.SUBSCRIBER_WAIT_PROVIDER_READY_MAX_RETRY
                or self.writer_failed
                or interrupt_thread_worker.is_set()
            ):
                burst_suppressed_logger.warning(
                    "timeout waiting provider starts caching or provider failed, abort"
                )
                self._acall_soon(output_que.put_nowait, _reader_failed_sentinel)
                return

            time.sleep(
                get_backoff(
                    _wait_count,
                    self.SUBSCRIBER_WAIT_BACKOFF_FACTOR,
                    self.SUBSCRIBER_WAIT_BACKOFF_MAX,
                )
            )
            _wait_count += 1

        buffer, bufferview = thread_local.buffer, thread_local.view
        # provider started, now we can actually subscribe the ongoing cache
        self._acall_soon(reader_ready_event.set)
        wait_data_count, bytes_read = 0, 0
        try:
            with open(self.fpath, "rb") as f:
                fd = f.fileno()
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)
                try:
                    while (
                        (not self.writer_failed)
                        or bytes_read < self._bytes_written
                        or not interrupt_thread_worker.is_set()
                    ):
                        _read_size = f.readinto(buffer)
                        if _read_size > 0:
                            wait_data_count = 0
                            bytes_read += _read_size
                            self._loop.call_soon_threadsafe(
                                output_que.put_nowait,
                                bufferview[:_read_size],  # type: ignore
                            )
                        # no data chunk is read, wait with backoff for the next
                        #   data chunk written by the provider.
                        elif (
                            wait_data_count <= self.SUBSCRIBER_WAIT_NEXT_CHUNK_MAX_RETRY
                        ):
                            time.sleep(
                                get_backoff(
                                    wait_data_count,
                                    self.SUBSCRIBER_WAIT_BACKOFF_FACTOR,
                                    self.SUBSCRIBER_WAIT_BACKOFF_MAX,
                                )
                            )
                            wait_data_count += 1
                        else:
                            # abort caching due to potential dead streaming coro
                            _err_msg = (
                                f"failed to read stream for {self.cache_meta}: "
                                "timeout getting data, partial read might happen"
                            )
                            burst_suppressed_logger.warning(_err_msg)
                        return
                finally:
                    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        except Exception:
            self._acall_soon(output_que.put_nowait, _reader_failed_sentinel)
        finally:
            self._acall_soon(output_que.put_nowait, None)
            del self, output_que  # del the ref to the tracker on finished


# a callback that register the cache entry indicates by input CacheMeta inst to the cache_db
_CACHE_ENTRY_REGISTER_CALLBACK = Callable[[CacheMeta], None]


class CachingRegister:
    """A tracker register that manages cache trackers.

    For each ongoing caching for unique OTA file, there will be only one unique identifier for it.

    This first caller that requests with the identifier will become the provider and create
    a new tracker for this identifier.
    The later comes callers will become the subscriber to this tracker.
    """

    def __init__(self):
        self._id_tracker: weakref.WeakValueDictionary[str, CacheTracker] = (
            weakref.WeakValueDictionary()
        )

    def get_tracker(self, cache_identifier: str) -> CacheTracker | None:
        """Get an inst of CacheTracker for the cache_identifier if existed.
        If the tracker doesn't exist, return a lock for tracker registeration.
        """
        _tracker = self._id_tracker.get(cache_identifier)
        if _tracker and not _tracker.writer_failed:
            return _tracker

    def register_tracker(self, cache_identifier: str, tracker: CacheTracker) -> None:
        """Create a register a new tracker into the register."""
        self._id_tracker[cache_identifier] = tracker


class CacheWriterPool:
    def __init__(
        self,
        *,
        max_workers: int = cfg.CACHE_WRITE_WORKERS_NUM,
        wait_on_busy: int = WAIT_WRITE_TIMEOUT,
    ) -> None:
        self.wait_on_busy = wait_on_busy
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="cache_writer"
        )

    async def close(self) -> None:
        await run_sync(self._pool.shutdown)

    async def stream_writing_cache(
        self,
        fd: AsyncGenerator[bytes],
        tracker: CacheTracker,
        cache_meta: CacheMeta,
    ) -> AsyncGenerator[bytes]:
        """A cache streamer that get data chunk from <fd> and tees to multiple destination.

        Data chunk yielded from <fd> will be teed to:
        1. upper uvicorn otaproxy APP to send back to client,
        2. cache_tracker cache_write_gen for caching to local.

        Returns:
            A AsyncGenerator[bytes] to yield data chunk from, for upper otaproxy uvicorn APP.

        Raises:
            CacheStreamingFailed if any exception happens.
        """
        tee_que: Queue[bytes | None] = Queue()
        wait_write_deadline = time.time() + self.wait_on_busy
        task = self._pool.submit(
            tracker.provider_write_file_in_thread, cache_meta, tee_que
        )
        try:
            # tee the incoming chunk to two destinations
            async for chunk in fd:
                # to caching task, if the tracker is still working
                if tracker._bytes_written < 0 and time.time() > wait_write_deadline:
                    _err_msg = (
                        f"timeout waiting an available writer: {wait_write_deadline=}"
                    )
                    burst_suppressed_logger.warning(_err_msg)
                    tracker.set_writer_failed()

                    try:
                        if task:
                            task.cancel()
                    except Exception:
                        pass

                if not tracker.writer_failed:
                    tee_que.put_nowait(chunk)

                # even cache write failed, we still continue pushing to uvicorn
                yield chunk
        except Exception as e:
            tracker.set_writer_failed()  # abort or drop caching
            _err_msg = f"upper file descriptor failed({cache_meta=}): {e!r}"
            burst_suppressed_logger.warning(_err_msg)
            raise CacheStreamingFailed(_err_msg) from e
        finally:
            # send sentinel to the caching task
            tee_que.put_nowait(None)
            # remove the refs
            fd, tracker, tee_que = None, None, None  # type: ignore


class CacheReaderPool:
    """A worker thread pool for cache reading."""

    def __init__(
        self,
        *,
        max_workers: int = cfg.CACHE_READ_WORKERS_NUM,
        wait_on_busy: int = WAIT_READ_TIMEOUT,
        read_chunk_size: int = cfg.LOCAL_READ_SIZE,
    ) -> None:
        self.wait_on_busy = wait_on_busy
        self.read_chunk_size = read_chunk_size
        self._worker_thread_local = threading.local()
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="cache_reader",
            initializer=self._thread_worker_initializer,
        )
        self._loop = asyncio.get_event_loop()
        self._acall_soon = self._loop.call_soon_threadsafe

    def _thread_worker_initializer(self) -> None:
        self._worker_thread_local.buffer = buffer = bytearray(cfg.CHUNK_SIZE)
        self._worker_thread_local.view = memoryview(buffer)

    async def close(self) -> None:
        await run_sync(self._pool.shutdown)

    def _read_file_in_thread(
        self,
        fpath: StrOrPath,
        output_que: asyncio.Queue[bytes | None | Any],
        *,
        ready_event: asyncio.Event,
        interrupt_thread_worker: threading.Event,
    ) -> None:
        # indicate caller that task is handled
        self._acall_soon(ready_event.set)
        try:
            buffer, bufferview = (
                self._worker_thread_local.buffer,
                self._worker_thread_local.view,
            )
            with open(fpath, "rb") as f:
                fd = f.fileno()
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)
                try:
                    while not interrupt_thread_worker.is_set():
                        _read_size = f.readinto(buffer)
                        if _read_size == 0:
                            return
                        self._acall_soon(output_que.put_nowait, bufferview[:_read_size])
                finally:
                    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        except Exception:
            self._acall_soon(output_que.put_nowait, _reader_failed_sentinel)
        finally:
            self._acall_soon(output_que.put_nowait, None)
            del self, output_que

    async def _stream_from_que(
        self,
        que: asyncio.Queue[bytes | None | Any],
        interrupt_thread_worker: threading.Event,
    ) -> AsyncGenerator[bytes]:
        """
        Raises:
            CacheStreamingInterrupt on reader failed.
        """
        try:
            while data := await que.get():
                if data is _reader_failed_sentinel:
                    raise CacheStreamingInterrupt("reader failed")
                yield data
        finally:
            interrupt_thread_worker.set()
            del que, self, interrupt_thread_worker

    async def read_file(self, fpath: StrOrPath) -> AsyncGenerator[bytes] | None:
        interrupt_thread_worker = threading.Event()
        ready_event = asyncio.Event()

        que = asyncio.Queue()
        task = self._pool.submit(
            self._read_file_in_thread,
            fpath,
            que,
            ready_event=ready_event,
            interrupt_thread_worker=interrupt_thread_worker,
        )
        try:
            await asyncio.wait_for(ready_event.wait(), self.wait_on_busy)
            return self._stream_from_que(que, interrupt_thread_worker)
        except asyncio.TimeoutError:
            task.cancel()
        finally:
            del que, task

    async def subscribe_tracker(
        self, tracker: CacheTracker
    ) -> AsyncGenerator[bytes] | None:
        interrupt_thread_worker = threading.Event()
        ready_event = asyncio.Event()

        que = asyncio.Queue()
        task = self._pool.submit(
            tracker.subscriber_stream_cache_in_thread,
            que,
            reader_ready_event=ready_event,
            interrupt_thread_worker=interrupt_thread_worker,
            thread_local=self._worker_thread_local,
        )
        try:
            await asyncio.wait_for(ready_event.wait(), self.wait_on_busy)
            return self._stream_from_que(que, interrupt_thread_worker)
        except asyncio.TimeoutError:
            task.cancel()
        finally:
            del que, task
