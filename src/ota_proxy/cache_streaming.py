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
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from queue import SimpleQueue
from typing import Any, AsyncGenerator, Callable, TypeVar

import anyio
from anyio.to_thread import run_sync
from typing_extensions import Concatenate, ParamSpec

from ota_proxy.utils import read_file_once
from otaclient_common._logging import get_burst_suppressed_logger
from otaclient_common._typing import StrOrPath
from otaclient_common.common import get_backoff

from .config import config as cfg
from .db import CacheMeta
from .errors import (
    CacheProviderNotReady,
    CacheStreamingFailed,
    CacheStreamingInterrupt,
    ReaderPoolBusy,
    StorageReachHardLimit,
)

logger = logging.getLogger(__name__)
# NOTE: for request_error, only allow max 6 lines of logging per 30 seconds
burst_suppressed_logger = get_burst_suppressed_logger(f"{__name__}.handle_error")

_reader_failed_sentinel = object()

P = ParamSpec("P")
RT = TypeVar("RT")

# cache tracker


def _unlink_no_error(fpath):
    try:
        os.unlink(fpath)
    except Exception:
        pass


class CacheTrackerEvents:
    def __init__(self):
        self._writer_started = threading.Event()
        self._writer_finished = threading.Event()
        self._writer_failed = threading.Event()

    @property
    def writer_failed(self) -> bool:
        return self._writer_failed.is_set()

    @property
    def writer_finished(self) -> bool:
        return self._writer_finished.is_set()

    @property
    def writer_started(self) -> bool:
        return self._writer_started.is_set()

    def set_writer_failed(self) -> None:  # pragma: no cover
        self._writer_finished.set()
        self._writer_failed.set()

    def set_writer_started(self) -> None:  # pragma: no cover
        self._writer_started.set()

    def set_writer_finished(self) -> None:  # pragma: no cover
        self._writer_finished.set()

    def on_deadline_set_failed(self, _) -> None:  # pragma: no cover
        if not self._writer_started.is_set():
            self._writer_failed.set()
            self._writer_finished.set()


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

    SUBSCRIBER_WAIT_PROVIDER_READY_MAX_RETRY = 16  # max wait: ~9s
    SUBSCRIBER_WAIT_NEXT_CHUNK_MAX_RETRY = 16  # max wait: ~9s
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
        base_dir: Path,
        commit_cache_cb: _CACHE_ENTRY_REGISTER_CALLBACK,
        below_hard_limit_event: threading.Event,
    ):
        self.meta_set = asyncio.Event()

        self.fpath = base_dir / self._tmp_file_naming(cache_identifier)
        self.save_path = base_dir / cache_identifier
        self._cache_meta: CacheMeta | None = None
        self._commit_cache_cb = commit_cache_cb

        self._tracker_events = CacheTrackerEvents()
        self._space_availability_event = below_hard_limit_event

        self._bytes_written = 0
        self._acall_soon = asyncio.get_event_loop().call_soon_threadsafe

    @property
    def cache_meta(self) -> CacheMeta | None:
        return self._cache_meta

    @cache_meta.setter
    def cache_meta(self, value: CacheMeta):
        self.meta_set.set()
        self._cache_meta = value

    def _finalize_cache(self) -> None:
        """Finalize the caching, commit the cache entry to db.

        At this point, the cache entry is already downloaded and complete.

        NOTE(20250731): when otaproxy starts with previous cache files existed,
                            there is chance that the `save_path` might already exist,
                            while not committed to the database.
                        Still commit the cache, if that file is broken, let otaclient
                            reports it with OTA cache file protocol in next OTA download request.
        NOTE(20250731): not considering the case that /ota-cache folder is tampered
                            or poluted intentionally, assume that all files are normal files.
                        If `save_dst` exists and is not a regular file, we just give up caching this file.
        """
        if not self.cache_meta:
            return

        if self.save_path.is_symlink():
            self.save_path.unlink()
        if self.save_path.exists():
            if not self.save_path.is_file():
                burst_suppressed_logger.warning(
                    f"potential poluted /ota-cache folder, {self.save_path} exists but not a file!"
                )
                return
        else:
            os.link(self.fpath, self.save_path)

        try:
            self._commit_cache_cb(self.cache_meta)
        except Exception as e:
            burst_suppressed_logger.warning(f"failed to commit cache to db: {e}")

    # exposed API

    def gen_deadline_checker(self):
        """Return a callable for deadline checker to set tracker failed if provider not enaged."""
        return self._tracker_events.on_deadline_set_failed

    def provider_write_file_in_thread(
        self, cache_meta: CacheMeta, input_que: SimpleQueue[bytes | None]
    ) -> None:
        tracker_events = self._tracker_events
        tracker_events.set_writer_started()
        try:
            with open(self.fpath, "wb") as f:
                fd = f.fileno()
                # first create the file
                f.write(b"")
                f.flush()
                os.fsync(fd)

                weakref.finalize(self, _unlink_no_error, self.fpath)
                try:
                    while data := input_que.get():
                        # caller set failed flag, or space hard limit is reached, abort
                        if tracker_events.writer_failed:
                            raise CacheStreamingFailed("upper data source failed")
                        if not self._space_availability_event.is_set():
                            _err_msg = f"abort caching {cache_meta} on space hard limit reached"
                            burst_suppressed_logger.warning(_err_msg)
                            raise StorageReachHardLimit(_err_msg)
                        f.write(data)
                        self._bytes_written += len(data)
                finally:
                    f.flush()
                    os.fsync(fd)
                    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)

            # NOTE(20240805): mark the writer succeeded in advance to release the
            #   subscriber faster. Whether the database entry is committed or not
            #   doesn't matter here, the subscriber doesn't need to fail if caching
            #   finished but db commit failed.
            tracker_events.set_writer_finished()
            cache_meta.cache_size = self._bytes_written

            if not tracker_events.writer_failed:
                self._finalize_cache()
        except Exception as e:
            tracker_events.set_writer_failed()
            burst_suppressed_logger.error(f"caching {cache_meta} failed: {e!r}")
        finally:
            tracker_events.set_writer_finished()
            del self, input_que, cache_meta

    def provider_write_once_in_thread(self, cache_meta: CacheMeta, data: bytes) -> None:
        tracker_events = self._tracker_events
        tracker_events.set_writer_started()
        try:
            if not self._space_availability_event.is_set():
                _err_msg = f"abort caching {cache_meta} on space hard limit reached"
                burst_suppressed_logger.warning(_err_msg)
                raise StorageReachHardLimit(_err_msg)

            with open(self.fpath, "wb") as f:
                fd = f.fileno()
                f.write(data)
                f.flush()
                os.fsync(fd)
                weakref.finalize(self, _unlink_no_error, self.fpath)
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            tracker_events.set_writer_finished()
            cache_meta.cache_size = self._bytes_written = len(data)
            self._finalize_cache()
        except Exception as e:
            tracker_events.set_writer_failed()
            burst_suppressed_logger.error(f"caching {cache_meta} failed: {e!r}")
        finally:
            tracker_events.set_writer_finished()
            del self, data, cache_meta

    async def subscriber_wait_for_provider(self) -> None:
        """
        Raises:
            CacheProviderNotReady if timeout waiting cache provider.
        """
        tracker_events = self._tracker_events
        _wait_count = 0
        try:
            while not tracker_events.writer_started:
                if (
                    _wait_count > self.SUBSCRIBER_WAIT_PROVIDER_READY_MAX_RETRY
                    or tracker_events.writer_failed
                ):
                    _err_msg = "timeout waiting provider starts caching or provider failed, abort"
                    burst_suppressed_logger.warning(_err_msg)
                    raise CacheProviderNotReady

                await asyncio.sleep(
                    get_backoff(
                        _wait_count,
                        self.SUBSCRIBER_WAIT_BACKOFF_FACTOR,
                        self.SUBSCRIBER_WAIT_BACKOFF_MAX,
                    )
                )
                _wait_count += 1
        finally:
            del self, tracker_events

    def subscriber_stream_cache_in_thread(
        self,
        output_que: asyncio.Queue[bytes | None | Any],
        *,
        interrupt_thread_worker: threading.Event,
        read_chunk_size: int,
    ) -> None:
        """Subscriber keeps polling chunks from ongoing tmp cache file.

        Subscriber will keep polling until the provider fails or
        provider finished and subscriber has read <bytes_written> bytes.
        """
        tracker_events = self._tracker_events
        wait_data_count, bytes_read = 0, 0
        try:
            with open(self.fpath, "rb") as f:
                fd = f.fileno()
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)
                try:
                    while (
                        not tracker_events.writer_finished
                        or bytes_read < self._bytes_written
                    ):
                        if (
                            interrupt_thread_worker.is_set()
                            or tracker_events.writer_failed
                        ):
                            return

                        if data := f.read(read_chunk_size):
                            wait_data_count = 0
                            bytes_read += len(data)
                            self._acall_soon(output_que.put_nowait, data)
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
                                "timeout getting data, potential partial read occurs"
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
        if _tracker and not _tracker._tracker_events.writer_failed:
            return _tracker

    def register_tracker(self, cache_identifier: str, tracker: CacheTracker) -> None:
        """Create a register a new tracker into the register."""
        self._id_tracker[cache_identifier] = tracker


class CacheWriterPool:
    def __init__(
        self,
        *,
        max_workers: int = cfg.CACHE_WRITE_WORKERS_NUM,
        max_pending: int = cfg.MAX_PENDING_WRITE,
    ) -> None:
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="cache_writer"
        )
        self._se = asyncio.Semaphore(max_pending)
        self._acall_soon = asyncio.get_event_loop().call_soon_threadsafe

    def _release_se_cb(self, _):
        self._acall_soon(self._se.release)

    async def _write_dispatcher(
        self,
        tracker: CacheTracker,
        fn: Callable[Concatenate[CacheMeta, P], Any],
        cache_meta: CacheMeta,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> None:
        tracker_event = tracker._tracker_events
        try:
            if self._se.locked():
                _err_msg = "exceed pending write tasks threshold, dropping caching"
                burst_suppressed_logger.warning(_err_msg)
                tracker_event.set_writer_failed()
                raise CacheStreamingFailed(_err_msg)

            await self._se.acquire()
            # NOTE(20250925): set the cache_meta to the tracker here
            tracker.cache_meta = cache_meta
            self._pool.submit(fn, cache_meta, *args, **kwargs).add_done_callback(
                self._release_se_cb
            )
        finally:
            del self, tracker, fn, args, kwargs

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
            CacheStreamingFailed if any exception occurs, or the writer pool queue is full.
        """
        tracker_event = tracker._tracker_events
        try:
            _first_chunk = await fd.__anext__()
            yield _first_chunk
        except StopAsyncIteration:
            # no data chunk from upper, might indicate an empty file
            await self._write_dispatcher(
                tracker,
                tracker._commit_cache_cb,
                cache_meta,
            )
            return

        # fast path for small resource that only takes one chunk's size
        try:
            _second_chunk = await fd.__anext__()
            yield _second_chunk
        except StopAsyncIteration:  # no next chunk
            await self._write_dispatcher(
                tracker,
                tracker.provider_write_once_in_thread,
                cache_meta,
                _first_chunk,
            )
            return  # only one chunk, directly write it and return

        tee_que: SimpleQueue[bytes | None] = SimpleQueue()
        tee_que.put_nowait(_first_chunk)
        tee_que.put_nowait(_second_chunk)
        await self._write_dispatcher(
            tracker,
            tracker.provider_write_file_in_thread,
            cache_meta,
            tee_que,
        )
        try:
            async for chunk in fd:
                if not tracker_event.writer_failed:
                    tee_que.put_nowait(chunk)
                yield chunk  # to uvicorn
        except Exception as e:
            tracker_event.set_writer_failed()  # hint thread worker to abort or drop caching
            _err_msg = f"upper file descriptor failed({cache_meta=}): {e!r}"
            burst_suppressed_logger.warning(_err_msg)
            raise CacheStreamingFailed(_err_msg) from e
        finally:
            tee_que.put_nowait(None)  # send sentinel to the caching task
            del fd, tracker, tee_que, tracker_event  # remove the refs


class CacheReaderPool:
    """A worker thread pool for cache reading."""

    def __init__(
        self,
        *,
        max_workers: int = cfg.CACHE_READ_WORKERS_NUM,
        max_pending: int = cfg.MAX_PENDING_READ,
        read_chunk_size: int = cfg.LOCAL_READ_SIZE,
    ) -> None:
        self.read_chunk_size = read_chunk_size
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="cache_reader"
        )
        self._se = asyncio.Semaphore(max_pending)
        self._acall_soon = asyncio.get_event_loop().call_soon_threadsafe

    def _release_se_cb(self, _):
        self._acall_soon(self._se.release)

    async def _read_dispatcher(
        self, fn: Callable[P, RT], *args: P.args, **kwargs: P.kwargs
    ) -> Future[RT]:
        """
        Raises:
            ReaderPoolBusy if exceeding max pending read tasks.
        """
        try:
            if self._se.locked():
                burst_suppressed_logger.warning(
                    "exceed pending read tasks threshold, dropping reading"
                )
                raise ReaderPoolBusy

            await self._se.acquire()
            _fut = self._pool.submit(fn, *args, **kwargs)
            _fut.add_done_callback(self._release_se_cb)
            return _fut
        finally:
            del self, fn, args, kwargs

    async def close(self) -> None:
        await run_sync(self._pool.shutdown)

    def _read_file_in_thread(
        self,
        fpath: StrOrPath | anyio.Path,
        output_que: asyncio.Queue[bytes | None | Any],
        *,
        interrupt_thread_worker: threading.Event,
    ) -> None:
        try:
            with open(fpath, "rb") as f:
                fd = f.fileno()
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)
                try:
                    while not interrupt_thread_worker.is_set() and (
                        data := f.read(self.read_chunk_size)
                    ):
                        self._acall_soon(output_que.put_nowait, data)
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
        """Stream data coming from read worker to upper caller.

        Read thread worker will use `_reader_failed_sentinel` to indicate this
            generator to interrupt when it fails.

        When this generator is interrupted by upper caller, it will on the other hand
            set the `interrupt_thread_worker` event to indicate thread worker.

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

    async def stream_read_file(
        self, fpath: StrOrPath | anyio.Path
    ) -> AsyncGenerator[bytes]:
        """For larger cache file, streaming reading the file.

        Raises:
            ReaderPoolBusy if exceeding max pending read tasks.
        """
        interrupt_thread_worker = threading.Event()
        que = asyncio.Queue()
        await self._read_dispatcher(
            self._read_file_in_thread,
            fpath,
            que,
            interrupt_thread_worker=interrupt_thread_worker,
        )
        return self._stream_from_que(que, interrupt_thread_worker)

    async def read_file_once(self, fpath: StrOrPath | anyio.Path) -> bytes:
        """For small cache file, directly read the whole file.

        Raises:
            ReaderPoolBusy if exceeding max pending read tasks.
        """
        return await asyncio.wrap_future(
            await self._read_dispatcher(read_file_once, fpath)
        )

    async def subscribe_tracker(self, tracker: CacheTracker) -> AsyncGenerator[bytes]:
        """
        Raises:
            ReaderPoolBusy if exceeding max pending read tasks.
            CacheProviderNotReady if timeout waiting cache provider ready.
        """
        # first we wait for provider ready
        await tracker.subscriber_wait_for_provider()

        interrupt_thread_worker = threading.Event()
        que = asyncio.Queue()
        # then wait for task picked by reader thread worker pool
        await self._read_dispatcher(
            tracker.subscriber_stream_cache_in_thread,
            que,
            interrupt_thread_worker=interrupt_thread_worker,
            read_chunk_size=self.read_chunk_size,
        )
        return self._stream_from_que(que, interrupt_thread_worker)
