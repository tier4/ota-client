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
import weakref
from typing import AsyncGenerator, Callable, Coroutine

import anyio
from anyio import open_file
from anyio.to_thread import run_sync

from otaclient_common._logging import get_burst_suppressed_logger
from otaclient_common.common import get_backoff

from .config import config as cfg
from .db import CacheMeta
from .errors import (
    CacheMultiStreamingFailed,
    CacheStreamingFailed,
    CacheStreamingInterrupt,
    StorageReachHardLimit,
)

logger = logging.getLogger(__name__)
# NOTE: for request_error, only allow max 6 lines of logging per 30 seconds
burst_suppressed_logger = get_burst_suppressed_logger(f"{__name__}.handle_error")

# cache tracker


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
        base_dir: anyio.Path,
        commit_cache_cb: _CACHE_ENTRY_REGISTER_CALLBACK,
        below_hard_limit_event: threading.Event,
    ):
        self.fpath = base_dir / self._tmp_file_naming(cache_identifier)
        self.save_path = base_dir / cache_identifier
        self.cache_meta: CacheMeta | None = None
        self._commit_cache_cb = commit_cache_cb

        self._writer_finished = asyncio.Event()
        self._writer_failed = asyncio.Event()

        self._space_availability_event = below_hard_limit_event

        self._bytes_written = 0

    @property
    def writer_failed(self) -> bool:
        return self._writer_failed.is_set()

    def set_writer_failed(self) -> None:  # pragma: no cover
        self._writer_finished.set()
        self._writer_failed.set()

    async def _subscriber_stream_cache(self) -> AsyncGenerator[bytes]:
        """Subscriber keeps polling chunks from ongoing tmp cache file.

        Subscriber will keep polling until the provider fails or
        provider finished and subscriber has read <bytes_written> bytes.

        Raises:
            CacheMultipleStreamingFailed if provider failed or timeout reading
            data chunk from tmp cache file(might be caused by a dead provider).
        """
        wait_data_count, bytes_read = 0, 0
        try:
            async with await open_file(self.fpath, "rb") as f:
                fd = f.wrapped.fileno()
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)
                while (
                    not self._writer_finished.is_set()
                    or bytes_read < self._bytes_written
                ):
                    if self._writer_failed.is_set():
                        raise CacheStreamingInterrupt(
                            f"abort reading stream on provider failed: {self.cache_meta}"
                        )

                    if _chunk := await f.read(cfg.CHUNK_SIZE):
                        wait_data_count = 0
                        bytes_read += len(_chunk)
                        yield _chunk
                    # no data chunk is read, wait with backoff for the next
                    #   data chunk written by the provider.
                    elif wait_data_count > self.SUBSCRIBER_WAIT_NEXT_CHUNK_MAX_RETRY:
                        # abort caching due to potential dead streaming coro
                        _err_msg = (
                            f"failed to read stream for {self.cache_meta}: "
                            "timeout getting data, partial read might happen"
                        )
                        logger.warning(_err_msg)
                        raise CacheMultiStreamingFailed(_err_msg)
                    else:
                        await asyncio.sleep(
                            get_backoff(
                                wait_data_count,
                                self.SUBSCRIBER_WAIT_BACKOFF_FACTOR,
                                self.SUBSCRIBER_WAIT_BACKOFF_MAX,
                            )
                        )
                        wait_data_count += 1
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            self = None  # del the ref to the tracker on finished

    # exposed API

    async def provider_write_cache(
        self, cache_meta: CacheMeta, input_que: asyncio.Queue[bytes]
    ) -> None:
        """Provider writes data chunks from upper caller to tmp cache file.

        If cache writing failed, this method will exit and tracker.writer_failed and
        tracker.writer_finished will be set.
        """
        self.cache_meta = cache_meta
        try:
            async with await open_file(self.fpath, "wb") as f:
                _written = 0
                while _data := await input_que.get():
                    if not self._space_availability_event.is_set():
                        _err_msg = f"abort writing cache for {cache_meta=}: {StorageReachHardLimit.__name__}"
                        burst_suppressed_logger.warning(_err_msg)
                        raise StorageReachHardLimit(_err_msg)

                    _written = await f.write(_data)
                    self._bytes_written += _written

                _fd = f.wrapped.fileno()
                await f.flush()
                await run_sync(os.fsync, _fd)
                os.posix_fadvise(_fd, 0, 0, os.POSIX_FADV_DONTNEED)

            # NOTE(20240805): mark the writer succeeded in advance to release the
            #   subscriber faster. Whether the database entry is committed or not
            #   doesn't matter here, the subscriber doesn't need to fail if caching
            #   finished but db commit failed.
            self._writer_finished.set()
            cache_meta.cache_size = self._bytes_written

            await self._commit_cache_cb(cache_meta)
            # finalize the cache file, always rewrite the existed file
            await run_sync(os.replace, self.fpath, self.save_path)
        except Exception as e:
            burst_suppressed_logger.warning(
                f"failed to write cache for {cache_meta=}: {e!r}"
            )
            await self.fpath.unlink(missing_ok=True)
            self._writer_failed.set()
        finally:
            # NOTE: always unblocked the subscriber waiting for writer ready/finished
            self._writer_finished.set()
            self, input_que = None, None  # type: ignore ,remove the ref to tracker

    async def subscribe_tracker(self) -> tuple[AsyncGenerator[bytes], CacheMeta] | None:
        """Subscribe to this tracker and get the cache stream and cache_meta."""
        _wait_count = 0
        while self._bytes_written <= 0 or self.cache_meta is None:
            if _wait_count > self.SUBSCRIBER_WAIT_PROVIDER_READY_MAX_RETRY:
                burst_suppressed_logger.warning(
                    "timeout waiting provider starts caching, abort"
                )
                return
            if self._writer_failed.is_set():
                return  # early break on failed provider

            await asyncio.sleep(
                get_backoff(
                    _wait_count,
                    self.SUBSCRIBER_WAIT_BACKOFF_FACTOR,
                    self.SUBSCRIBER_WAIT_BACKOFF_MAX,
                )
            )
            _wait_count += 1

        if not self._writer_failed.is_set() and self.cache_meta:
            return self._subscriber_stream_cache(), self.cache_meta


# a callback that register the cache entry indicates by input CacheMeta inst to the cache_db
_CACHE_ENTRY_REGISTER_CALLBACK = Callable[[CacheMeta], Coroutine[None, None, None]]


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


async def cache_streaming(
    fd: AsyncGenerator[bytes], tracker: CacheTracker, cache_meta: CacheMeta
) -> AsyncGenerator[bytes]:
    """A cache streamer that get data chunk from <fd> and tees to multiple destination.

    Data chunk yielded from <fd> will be teed to:
    1. upper uvicorn otaproxy APP to send back to client,
    2. cache_tracker cache_write_gen for caching to local.

    Args:
        fd: opened connection to a remote file.
        tracker: an inst of ongoing cache tracker bound to this request.
        cache_meta: meta data of the requested resource.

    Returns:
        A AsyncGenerator[bytes] to yield data chunk from, for upper otaproxy uvicorn APP.

    Raises:
        CacheStreamingFailed if any exception happens.
    """
    tee_que: asyncio.Queue[bytes] = asyncio.Queue()
    try:
        asyncio.create_task(tracker.provider_write_cache(cache_meta, tee_que))

        # tee the incoming chunk to two destinations
        async for chunk in fd:
            # NOTE: for aiohttp, when HTTP chunk encoding is enabled,
            #       an empty chunk will be sent to indicate the EOF of stream,
            #       we MUST handle this empty chunk.
            if not chunk:  # skip if empty chunk is read from remote
                continue

            # to caching task, if the tracker is still working
            if not tracker._writer_failed.is_set():
                tee_que.put_nowait(chunk)

            # even cache write failed, we still continue pushing to uvicorn
            yield chunk
    except Exception as e:
        _err_msg = f"upper file descriptor failed({cache_meta=}): {e!r}"
        burst_suppressed_logger.warning(_err_msg)
        raise CacheStreamingFailed(_err_msg) from e
    finally:
        # send sentinel to the caching task
        tee_que.put_nowait(None)  # type: ignore

        # remove the refs
        fd, tracker, tee_que = None, None, None  # type: ignore
