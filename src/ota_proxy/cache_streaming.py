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
import contextlib
import logging
import os
import threading
import weakref
from concurrent.futures import Executor
from pathlib import Path
from typing import (
    AsyncGenerator,
    AsyncIterator,
    Callable,
    Coroutine,
    Generic,
    List,
    MutableMapping,
    Optional,
    Tuple,
    TypeVar,
    Union,
)

import aiofiles

from .config import config as cfg
from .db import CacheMeta
from .errors import (
    CacheMultiStreamingFailed,
    CacheStreamingInterrupt,
    StorageReachHardLimit,
)
from .utils import wait_with_backoff

logger = logging.getLogger(__name__)

_WEAKREF = TypeVar("_WEAKREF")

# cache tracker


class CacheTracker(Generic[_WEAKREF]):
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

    READER_SUBSCRIBE_WAIT_PROVIDER_TIMEOUT = 2
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
        ref_holder: _WEAKREF,
        *,
        base_dir: Union[str, Path],
        executor: Executor,
        callback: _CACHE_ENTRY_REGISTER_CALLBACK,
        below_hard_limit_event: threading.Event,
    ):
        self.fpath = Path(base_dir) / self._tmp_file_naming(cache_identifier)
        self.meta: CacheMeta = None  # type: ignore[assignment]
        self.cache_identifier = cache_identifier
        self.save_path = Path(base_dir) / cache_identifier
        self._writer_ready = asyncio.Event()
        self._writer_finished = asyncio.Event()
        self._writer_failed = asyncio.Event()
        self._ref = ref_holder
        self._subscriber_ref_holder: List[_WEAKREF] = []
        self._executor = executor
        self._cache_commit_cb = callback
        self._space_availability_event = below_hard_limit_event

        self._bytes_written = 0
        self._cache_write_gen: Optional[AsyncGenerator[int, bytes]] = None

        # self-register the finalizer to this tracker
        weakref.finalize(
            self,
            self.finalizer,
            fpath=self.fpath,
        )

    async def _finalize_caching(self):
        """Commit cache entry to db and rename tmp cached file with sha256hash
        to fialize the caching."""
        # if the file with the same sha256has is already presented, skip the hardlink
        # NOTE: no need to clean the tmp file, it will be done by the cache tracker.
        await self._cache_commit_cb(self.meta)
        if not self.save_path.is_file():
            self.fpath.link_to(self.save_path)

    @staticmethod
    def finalizer(*, fpath: Union[str, Path]):
        """Finalizer that cleans up the tmp file when this tracker is gced."""
        Path(fpath).unlink(missing_ok=True)

    @property
    def writer_failed(self) -> bool:
        return self._writer_failed.is_set()

    @property
    def writer_finished(self) -> bool:
        return self._writer_finished.is_set()

    @property
    def is_cache_valid(self) -> bool:
        """Indicates whether the temp cache entry for this tracker is valid."""
        return (
            self.meta is not None
            and self._writer_finished.is_set()
            and not self._writer_failed.is_set()
        )

    def get_cache_write_gen(self) -> Optional[AsyncGenerator[int, bytes]]:
        if self._cache_write_gen:
            return self._cache_write_gen
        logger.warning(f"tracker for {self.meta} is not ready, skipped")

    async def _provider_write_cache(self) -> AsyncGenerator[int, bytes]:
        """Provider writes data chunks from upper caller to tmp cache file.

        If cache writing failed, this method will exit and tracker.writer_failed and
        tracker.writer_finished will be set.
        """
        logger.debug(f"start to cache for {self.meta=}...")
        try:
            if not self.meta:
                raise ValueError("called before provider tracker is ready, abort")

            async with aiofiles.open(self.fpath, "wb", executor=self._executor) as f:
                # let writer become ready when file is open successfully
                self._writer_ready.set()

                _written = 0
                while _data := (yield _written):
                    if not self._space_availability_event.is_set():
                        logger.warning(
                            f"abort writing cache for {self.meta=}: {StorageReachHardLimit.__name__}"
                        )
                        self._writer_failed.set()
                        return

                    _written = await f.write(_data)
                    self._bytes_written += _written

            logger.debug(
                "cache write finished, total bytes written"
                f"({self._bytes_written}) for {self.meta=}"
            )
            self.meta.cache_size = self._bytes_written

            # NOTE: no need to track the cache commit,
            #       as writer_finish is meant to set on cache file created.
            asyncio.create_task(self._finalize_caching())
        except Exception as e:
            logger.exception(f"failed to write cache for {self.meta=}: {e!r}")
            self._writer_failed.set()
        finally:
            self._writer_finished.set()
            self._ref = None

    async def _subscribe_cache_streaming(self) -> AsyncIterator[bytes]:
        """Subscriber keeps polling chunks from ongoing tmp cache file.

        Subscriber will keep polling until the provider fails or
        provider finished and subscriber has read <bytes_written> bytes.

        Raises:
            CacheMultipleStreamingFailed if provider failed or timeout reading
            data chunk from tmp cache file(might be caused by a dead provider).
        """
        try:
            err_count, _bytes_read = 0, 0
            async with aiofiles.open(self.fpath, "rb", executor=self._executor) as f:
                while not (self.writer_finished and _bytes_read == self._bytes_written):
                    if self.writer_failed:
                        raise CacheMultiStreamingFailed(
                            f"provider aborted for {self.meta}"
                        )
                    _bytes_read += len(_chunk := await f.read(cfg.CHUNK_SIZE))
                    if _chunk:
                        err_count = 0
                        yield _chunk
                        continue

                    err_count += 1
                    if not await wait_with_backoff(
                        err_count,
                        _backoff_factor=cfg.STREAMING_BACKOFF_FACTOR,
                        _backoff_max=cfg.STREAMING_BACKOFF_MAX,
                    ):
                        # abort caching due to potential dead streaming coro
                        _err_msg = f"failed to stream({self.meta=}): timeout getting data, partial read might happen"
                        logger.error(_err_msg)
                        # signal streamer to stop streaming
                        raise CacheMultiStreamingFailed(_err_msg)
        finally:
            # unsubscribe on finish
            self._subscriber_ref_holder.pop()

    async def _read_cache(self) -> AsyncIterator[bytes]:
        """Directly open the tmp cache entry and yield data chunks from it.

        Raises:
            CacheMultipleStreamingFailed if fails to read <self._bytes_written> from the
            cached file, this might indicate a partial written cache file.
        """
        _bytes_read, _retry_count = 0, 0
        async with aiofiles.open(self.fpath, "rb", executor=self._executor) as f:
            while _bytes_read < self._bytes_written:
                if _data := await f.read(cfg.CHUNK_SIZE):
                    _retry_count = 0
                    _bytes_read += len(_data)
                    yield _data
                    continue

                # no data is read from the cache entry,
                # retry sometimes to ensure all data is acquired
                _retry_count += 1
                if not await wait_with_backoff(
                    _retry_count,
                    _backoff_factor=cfg.STREAMING_BACKOFF_FACTOR,
                    _backoff_max=cfg.STREAMING_CACHED_TMP_TIMEOUT,
                ):
                    # abort caching due to potential dead streaming coro
                    _err_msg = (
                        f"open_cached_tmp failed for ({self.meta=}): "
                        "timeout getting more data, partial written cache file detected"
                    )
                    logger.debug(_err_msg)
                    # signal streamer to stop streaming
                    raise CacheMultiStreamingFailed(_err_msg)

    # exposed API

    async def provider_start(self, meta: CacheMeta):
        """Register meta to the Tracker, create tmp cache entry and get ready.

        Check _provider_write_cache for more details.

        Args:
            meta: inst of CacheMeta for the requested file tracked by this tracker.
                This meta is created by open_remote() method.
        """
        self.meta = meta
        self._cache_write_gen = self._provider_write_cache()
        await self._cache_write_gen.asend(None)  # type: ignore

    async def provider_on_finished(self):
        if not self.writer_finished and self._cache_write_gen:
            with contextlib.suppress(StopAsyncIteration):
                await self._cache_write_gen.asend(b"")
        self._writer_finished.set()
        self._ref = None

    async def provider_on_failed(self):
        """Manually fail and stop the caching."""
        if not self.writer_finished and self._cache_write_gen:
            logger.warning(f"interrupt writer coroutine for {self.meta=}")
            with contextlib.suppress(StopAsyncIteration, CacheStreamingInterrupt):
                await self._cache_write_gen.athrow(CacheStreamingInterrupt)

        self._writer_failed.set()
        self._writer_finished.set()
        self._ref = None

    async def subscriber_subscribe_tracker(self) -> Optional[AsyncIterator[bytes]]:
        """Reader subscribe this tracker and get a file descriptor to get data chunks."""
        _wait_count = 0
        while not self._writer_ready.is_set():
            _wait_count += 1
            if self.writer_failed or not await wait_with_backoff(
                _wait_count,
                _backoff_factor=cfg.STREAMING_BACKOFF_FACTOR,
                _backoff_max=self.READER_SUBSCRIBE_WAIT_PROVIDER_TIMEOUT,
            ):
                return  # timeout waiting for provider to become ready

        # subscribe on an ongoing cache
        if not self.writer_finished and isinstance(self._ref, _Weakref):
            self._subscriber_ref_holder.append(self._ref)
            return self._subscribe_cache_streaming()
        # caching just finished, try to directly read the finished cache entry
        elif self.is_cache_valid:
            return self._read_cache()


# a callback that register the cache entry indicates by input CacheMeta inst to the cache_db
_CACHE_ENTRY_REGISTER_CALLBACK = Callable[[CacheMeta], Coroutine[None, None, None]]


class _Weakref:
    pass


class CachingRegister:
    """A tracker register that manages cache trackers.

    For each ongoing caching for unique OTA file, there will be only one unique identifier for it.

    This first caller that requests with the identifier will become the provider and create
    a new tracker for this identifier.
    The later comes callers will become the subscriber to this tracker.
    """

    def __init__(self, base_dir: Union[str, Path]):
        self._base_dir = Path(base_dir)
        self._id_ref_dict: MutableMapping[str, _Weakref] = weakref.WeakValueDictionary()
        self._ref_tracker_dict: MutableMapping[_Weakref, CacheTracker] = (
            weakref.WeakKeyDictionary()
        )

    async def get_tracker(
        self,
        cache_identifier: str,
        *,
        executor: Executor,
        callback: _CACHE_ENTRY_REGISTER_CALLBACK,
        below_hard_limit_event: threading.Event,
    ) -> Tuple[CacheTracker, bool]:
        """Get an inst of CacheTracker for the cache_identifier.

        Returns:
            An inst of tracker, and a bool indicates the caller is provider(True),
                or subscriber(False).
        """
        _new_ref = _Weakref()
        _ref = self._id_ref_dict.setdefault(cache_identifier, _new_ref)

        # subscriber
        if (
            _tracker := self._ref_tracker_dict.get(_ref)
        ) and not _tracker.writer_failed:
            return _tracker, False

        # provider, or override a failed provider
        if _ref is not _new_ref:  # override a failed tracker
            self._id_ref_dict[cache_identifier] = _new_ref
            _ref = _new_ref

        _tracker = CacheTracker(
            cache_identifier,
            _ref,
            base_dir=self._base_dir,
            executor=executor,
            callback=callback,
            below_hard_limit_event=below_hard_limit_event,
        )
        self._ref_tracker_dict[_ref] = _tracker
        return _tracker, True
