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
import os
import aiofiles
import aiohttp
import bisect
import logging
import shutil
import time
import threading
import weakref
from concurrent.futures import Executor, ThreadPoolExecutor
from multidict import CIMultiDictProxy
from pathlib import Path
from typing import (
    AsyncGenerator,
    AsyncIterator,
    Callable,
    Coroutine,
    Dict,
    Generic,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Tuple,
    TypeVar,
    Union,
)
from urllib.parse import SplitResult, quote, urlsplit

from ._consts import HEADER_OTA_FILE_CACHE_CONTROL, HEADER_CONTENT_ENCODING
from .cache_control import OTAFileCacheControl
from .db import CacheMeta, OTACacheDB, AIO_OTACacheDBProxy
from .errors import (
    BaseOTACacheError,
    CacheStreamingFailed,
    CacheMultiStreamingFailed,
    CacheStreamingInterrupt,
    StorageReachHardLimit,
)
from .config import config as cfg
from .utils import url_based_hash, wait_with_backoff, read_file

logger = logging.getLogger(__name__)


_WEAKREF = TypeVar("_WEAKREF")


# helper functions


def create_cachemeta_for_request(
    raw_url: str,
    cache_identifier: str,
    compression_alg: str,
    /,
    resp_headers_from_upper: CIMultiDictProxy[str],
) -> CacheMeta:
    """Create CacheMeta inst for new incoming request.

    Use information from upper in prior, otherwise use pre-calculated information.

    Params:
        raw_url
        cache_identifier: pre-collected information from caller
        compression_alg: pre-collected information from caller
        resp_headers_from_upper
    """
    cache_meta = CacheMeta(
        url=raw_url,
        content_encoding=resp_headers_from_upper.get(HEADER_CONTENT_ENCODING, ""),
    )

    _upper_cache_policy = OTAFileCacheControl.parse_header(
        resp_headers_from_upper.get(HEADER_OTA_FILE_CACHE_CONTROL, "")
    )
    if _upper_cache_policy.file_sha256:
        cache_meta.file_sha256 = _upper_cache_policy.file_sha256
        cache_meta.file_compression_alg = _upper_cache_policy.file_compression_alg
    else:
        cache_meta.file_sha256 = cache_identifier
        cache_meta.file_compression_alg = compression_alg
    return cache_meta


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
            try:
                await self._cache_write_gen.asend(b"")
            except StopAsyncIteration:
                pass
        self._writer_finished.set()
        self._ref = None

    async def provider_on_failed(self):
        """Manually fail and stop the caching."""
        if not self.writer_finished and self._cache_write_gen:
            logger.warning(f"interrupt writer coroutine for {self.meta=}")
            try:
                await self._cache_write_gen.athrow(CacheStreamingInterrupt)
            except (StopAsyncIteration, CacheStreamingInterrupt):
                pass

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
        self._ref_tracker_dict: MutableMapping[
            _Weakref, CacheTracker
        ] = weakref.WeakKeyDictionary()

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


class LRUCacheHelper:
    """A helper class that provides API for accessing/managing cache entries in ota cachedb.

    Serveral buckets are created according to predefined file size threshould.
    Each bucket will maintain the cache entries of that bucket's size definition,
    LRU is applied on per-bucket scale.

    NOTE: currently entry in first bucket and last bucket will skip LRU rotate.
    """

    BSIZE_LIST = list(cfg.BUCKET_FILE_SIZE_DICT.keys())
    BSIZE_DICT = cfg.BUCKET_FILE_SIZE_DICT

    def __init__(self, db_f: Union[str, Path]):
        self._db = AIO_OTACacheDBProxy(db_f)
        self._closed = False

    def close(self):
        if not self._closed:
            self._db.close()

    async def commit_entry(self, entry: CacheMeta) -> bool:
        """Commit cache entry meta to the database."""
        # populate bucket and last_access column
        entry.bucket_idx = bisect.bisect_right(self.BSIZE_LIST, entry.cache_size) - 1
        entry.last_access = int(time.time())

        if (await self._db.insert_entry(entry)) != 1:
            logger.error(f"db: failed to add {entry=}")
            return False
        return True

    async def lookup_entry(self, file_sha256: str) -> Optional[CacheMeta]:
        return await self._db.lookup_entry(CacheMeta.file_sha256, file_sha256)

    async def remove_entry(self, file_sha256: str) -> bool:
        return (await self._db.remove_entries(CacheMeta.file_sha256, file_sha256)) > 0

    async def rotate_cache(self, size: int) -> Optional[List[str]]:
        """Wrapper method for calling the database LRU cache rotating method.

        Args:
            size int: the size of file that we want to reserve space for

        Returns:
            A list of hashes that needed to be cleaned, or empty list if rotation
                is not required, or None if cache rotation cannot be executed.
        """
        # NOTE: currently item size smaller than 1st bucket and larger than latest bucket
        #       will be saved without cache rotating.
        if size >= self.BSIZE_LIST[-1] or size < self.BSIZE_LIST[1]:
            return []

        _cur_bucket_idx = bisect.bisect_right(self.BSIZE_LIST, size) - 1
        _cur_bucket_size = self.BSIZE_LIST[_cur_bucket_idx]

        # first check the upper bucket, remove 1 item from any of the
        # upper bucket is enough.
        for _bucket_idx in range(_cur_bucket_idx + 1, len(self.BSIZE_LIST)):
            if res := await self._db.rotate_cache(_bucket_idx, 1):
                return res
        # if cannot find one entry at any upper bucket, check current bucket
        return await self._db.rotate_cache(
            _cur_bucket_idx, self.BSIZE_DICT[_cur_bucket_size]
        )


async def cache_streaming(
    fd: AsyncIterator[bytes],
    meta: CacheMeta,
    tracker: CacheTracker,
) -> AsyncIterator[bytes]:
    """A cache streamer that get data chunk from <fd> and tees to multiple destination.

    Data chunk yielded from <fd> will be teed to:
    1. upper uvicorn otaproxy APP to send back to client,
    2. cache_tracker cache_write_gen for caching.

    Args:
        fd: opened connection to a remote file.
        meta: meta data of the requested resource.
        tracker: an inst of ongoing cache tracker bound to this request.

    Returns:
        A bytes async iterator to yield data chunk from, for upper otaproxy uvicorn APP.

    Raises:
        CacheStreamingFailed if any exception happens during retrieving.
    """

    async def _inner():
        _cache_write_gen = tracker.get_cache_write_gen()
        try:
            # tee the incoming chunk to two destinations
            async for chunk in fd:
                # NOTE: for aiohttp, when HTTP chunk encoding is enabled,
                #       an empty chunk will be sent to indicate the EOF of stream,
                #       we MUST handle this empty chunk.
                if not chunk:  # skip if empty chunk is read from remote
                    continue
                # to caching generator
                if _cache_write_gen and not tracker.writer_finished:
                    try:
                        await _cache_write_gen.asend(chunk)
                    except Exception as e:
                        await tracker.provider_on_failed()  # signal tracker
                        logger.error(
                            f"cache write coroutine failed for {meta=}, abort caching: {e!r}"
                        )
                # to uvicorn thread
                yield chunk
            await tracker.provider_on_finished()
        except Exception as e:
            logger.exception(f"cache tee failed for {meta=}")
            await tracker.provider_on_failed()
            raise CacheStreamingFailed from e

    await tracker.provider_start(meta)
    return _inner()


class OTACache:
    """Maintain caches for requested remote OTA files.

    Instance of this class handles the request from the upper caller,
    proxying the requests to the remote ota files server, caching the OTA files
    and sending data chunks back to the upper caller.
    If cache is available for specific URL, it will handle the request using local caches.

    Attributes:
        upper_proxy: the upper proxy that ota_cache uses to send out request, default is None
        cache_enabled: when set to False, ota_cache will only relay requested data, default is False.
        enable_https: whether the ota_cache should send out the requests with HTTPS,
            default is False. NOTE: scheme change is applied unconditionally.
        init_cache: whether to clear the existed cache, default is True.
        base_dir: the location to store cached files.
        db_file: the location to store database file.
    """

    def __init__(
        self,
        *,
        cache_enabled: bool,
        init_cache: bool,
        base_dir: Optional[Union[str, Path]] = None,
        db_file: Optional[Union[str, Path]] = None,
        upper_proxy: str = "",
        enable_https: bool = False,
        external_cache: Optional[str] = None,
    ):
        """Init ota_cache instance with configurations."""
        logger.info(
            f"init ota_cache({cache_enabled=}, {init_cache=}, {upper_proxy=}, {enable_https=})"
        )
        self._closed = True

        self._chunk_size = cfg.CHUNK_SIZE
        self._base_dir = Path(base_dir) if base_dir else Path(cfg.BASE_DIR)
        self._db_file = Path(db_file) if db_file else Path(cfg.DB_FILE)
        self._cache_enabled = cache_enabled
        self._init_cache = init_cache
        self._enable_https = enable_https
        self._executor = ThreadPoolExecutor(thread_name_prefix="ota_cache_executor")

        if external_cache and cache_enabled:
            logger.info(f"external cache source is enabled at: {external_cache}")
        self._external_cache = Path(external_cache) if external_cache else None

        self._storage_below_hard_limit_event = threading.Event()
        self._storage_below_soft_limit_event = threading.Event()
        self._upper_proxy = upper_proxy

    def _check_cache_db(self) -> bool:
        """Check ota_cache can be reused or not."""
        return (
            self._base_dir.is_dir()
            and self._db_file.is_file()
            and OTACacheDB.check_db_file(self._db_file)
        )

    async def start(self):
        """Start the ota_cache instance."""
        # silently ignore multi launching of ota_cache
        if not self._closed:
            logger.warning("try to launch already launched ota_cache instance, ignored")
            return
        self._closed = False
        self._base_dir.mkdir(exist_ok=True, parents=True)

        # NOTE: we configure aiohttp to not decompress the resp body(if content-encoding specified),
        #       we cache the contents as its original form, and send to the client with
        #       the same content-encoding headers to indicate the client to compress the
        #       resp body by their own.
        # NOTE 2: disable aiohttp default timeout(5mins)
        #       this timeout will be applied to the whole request, including downloading,
        #       preventing large files to be downloaded.
        timeout = aiohttp.ClientTimeout(
            total=None, sock_read=cfg.AIOHTTP_SOCKET_READ_TIMEOUT
        )
        self._session = aiohttp.ClientSession(
            auto_decompress=False, raise_for_status=True, timeout=timeout
        )

        if self._cache_enabled:
            # purge cache dir if requested(init_cache=True) or ota_cache invalid,
            #   and then recreate the cache folder and cache db file.
            db_f_valid = self._check_cache_db()
            if self._init_cache or not db_f_valid:
                logger.warning(
                    f"purge and init ota_cache: {self._init_cache=}, {db_f_valid}"
                )
                shutil.rmtree(str(self._base_dir), ignore_errors=True)
                self._base_dir.mkdir(exist_ok=True, parents=True)
                # init db file with table created
                OTACacheDB.init_db_file(self._db_file)
            # reuse the previously left ota_cache
            else:  # cleanup unfinished tmp files
                for tmp_f in self._base_dir.glob(f"{cfg.TMP_FILE_PREFIX}*"):
                    tmp_f.unlink(missing_ok=True)

            # dispatch a background task to pulling the disk usage info
            self._executor.submit(self._background_check_free_space)

            # init cache helper(and connect to ota_cache db)
            self._lru_helper = LRUCacheHelper(self._db_file)
            self._on_going_caching = CachingRegister(self._base_dir)

            if self._upper_proxy:
                # if upper proxy presented, force disable https
                self._enable_https = False

        logger.info("ota_cache started")

    async def close(self):
        """Shutdowns OTACache instance.

        NOTE: cache folder cleanup on successful ota update is not
            performed by the OTACache.
        """
        logger.debug("shutdown ota-cache...")
        if self._cache_enabled and not self._closed:
            self._closed = True
            await self._session.close()
            self._lru_helper.close()
            self._executor.shutdown(wait=True)

        logger.info("shutdown ota-cache completed")

    def _background_check_free_space(self):
        """Constantly checks the usage of disk where cache folder residented.

        This method keep loop querying the disk usage.
        There are 2 types of threshold defined here, hard limit and soft limit:
        1. soft limit:
            When disk usage reaching soft limit, OTACache will reserve free space(size of the entry)
            for newly cached entry by deleting old cached entries in LRU flavour.
            If the reservation of free space failed, the newly cached entry will be deleted.
        2. hard limit:
            When disk usage reaching hard limit, OTACache will stop caching any new entries.

        Raises:
            Raises FileNotFoundError if the cache folder somehow disappears during checking.
        """
        while not self._closed:
            try:
                disk_usage = shutil.disk_usage(self._base_dir)
            except FileNotFoundError:
                logger.error(
                    "background free space check interrupted as cache folder disappeared,"
                    "this is treated the same as storage reached hard limit."
                )
                self._storage_below_soft_limit_event.clear()
                self._storage_below_hard_limit_event.clear()
                break

            current_used_p = disk_usage.used / disk_usage.total * 100

            _previous_below_hard = self._storage_below_hard_limit_event.is_set()
            _current_below_hard = True
            if current_used_p <= cfg.DISK_USE_LIMIT_SOFT_P:
                self._storage_below_soft_limit_event.set()
                self._storage_below_hard_limit_event.set()
            elif current_used_p <= cfg.DISK_USE_LIMIT_HARD_P:
                self._storage_below_soft_limit_event.clear()
                self._storage_below_hard_limit_event.set()
            else:
                self._storage_below_soft_limit_event.clear()
                self._storage_below_hard_limit_event.clear()
                _current_below_hard = False

            # logging space monitoring result
            if _previous_below_hard and not _current_below_hard:
                logger.warning(
                    f"disk usage reached hard limit({current_used_p=:.1}%,"
                    f"{cfg.DISK_USE_LIMIT_HARD_P:.1}%), cache disabled"
                )
            elif not _previous_below_hard and _current_below_hard:
                logger.info(
                    f"disk usage is below hard limit({current_used_p=:.1}%),"
                    f"{cfg.DISK_USE_LIMIT_SOFT_P:.1}%), cache enabled again"
                )
            time.sleep(cfg.DISK_USE_PULL_INTERVAL)

    def _cache_entries_cleanup(self, entry_hashes: List[str]):
        """Cleanup entries indicated by entry_hashes list."""
        for entry_hash in entry_hashes:
            # remove cache entry
            f = self._base_dir / entry_hash
            f.unlink(missing_ok=True)

    async def _reserve_space(self, size: int) -> bool:
        """A helper that calls lru_helper's rotate_cache method.

        Args:
            size: the size of the target that we need to reserve space for

        Returns:
            A bool indicates whether the space reserving is successful or not.
        """
        _hashes = await self._lru_helper.rotate_cache(size)
        # NOTE: distinguish between [] and None! The fore one means we don't need
        #       cache rotation for saving the cache file, the latter one means
        #       cache rotation failed.
        if _hashes is not None:
            logger.debug(
                f"rotate on bucket({size=}), num of entries to be cleaned {len(_hashes)=}"
            )
            self._executor.submit(self._cache_entries_cleanup, _hashes)
            return True
        else:
            logger.debug(f"rotate on bucket({size=}) failed, no enough entries")
            return False

    async def _commit_cache_callback(self, meta: CacheMeta):
        """The callback for committing CacheMeta to cache_db.

        If caching is successful, and the space usage is reaching soft limit,
        we will try to ensure free space for already cached file.
        If space cannot be reserved, the meta will not be committed.

        Args:
            meta: inst of CacheMeta that represents a cached file.
        """
        try:
            if not self._storage_below_soft_limit_event.is_set():
                # case 1: try to reserve space for the saved cache entry
                if await self._reserve_space(meta.cache_size):
                    if not await self._lru_helper.commit_entry(meta):
                        logger.debug(f"failed to commit cache for {meta.url=}")
                else:
                    # case 2: cache successful, but reserving space failed,
                    # NOTE(20221018): let cache tracker remove the tmp file
                    logger.debug(f"failed to reserve space for {meta.url=}")
            else:
                # case 3: commit cache and finish up
                if not await self._lru_helper.commit_entry(meta):
                    logger.debug(f"failed to commit cache entry for {meta.url=}")
        except Exception as e:
            logger.exception(f"failed on callback for {meta=}: {e!r}")

    def _process_raw_url(self, raw_url: str) -> str:
        """Process the raw URL received from upper uvicorn app.

        NOTE: raw_url(get from uvicorn) is unquoted, we must quote it again before we send it to the remote
        NOTE(20221003): as otaproxy, we should treat all contents after netloc as path and not touch it,
                        because we should forward the request as it to the remote.
        NOTE(20221003): unconditionally set scheme to https if enable_https, else unconditionally set to http
        """
        _raw_parse = urlsplit(raw_url)
        # get the base of the raw_url, which is <scheme>://<netloc>
        _raw_base = SplitResult(
            scheme=_raw_parse.scheme,
            netloc=_raw_parse.netloc,
            path="",
            query="",
            fragment="",
        ).geturl()

        # get the leftover part of URL besides base as path, and then quote it
        # finally, regenerate proper quoted url
        return SplitResult(
            scheme="https" if self._enable_https else "http",
            netloc=_raw_parse.netloc,
            path=quote(raw_url.replace(_raw_base, "", 1)),
            query="",
            fragment="",
        ).geturl()

    # retrieve_file handlers

    async def _retrieve_file_by_downloading(
        self,
        raw_url: str,
        *,
        headers: Mapping[str, str],
    ) -> Tuple[AsyncIterator[bytes], CIMultiDictProxy[str]]:
        async def _do_request() -> AsyncIterator[bytes]:
            async with self._session.get(
                self._process_raw_url(raw_url),
                proxy=self._upper_proxy,
                headers=headers,
            ) as response:
                yield response.headers  # type: ignore
                # NOTE(20230803): sometimes aiohttp will raises:
                #                 "ClientPayloadError: Response payload is not completed" exception,
                #                 according to some posts in related issues, add a asyncio.sleep(0)
                #                 to make event loop switch to other task here seems mitigates the problem.
                #                 check the following URLs for details:
                #                 1. https://github.com/aio-libs/aiohttp/issues/4581
                #                 2. https://docs.python.org/3/library/asyncio-task.html#sleeping
                await asyncio.sleep(0)
                async for data, _ in response.content.iter_chunks():
                    if data:  # only yield non-empty data chunk
                        yield data

        # open remote connection
        resp_headers: CIMultiDictProxy[str] = await (_remote_fd := _do_request()).__anext__()  # type: ignore
        return _remote_fd, resp_headers

    async def _retrieve_file_by_cache(
        self, cache_identifier: str, *, retry_cache: bool
    ) -> Optional[Tuple[AsyncIterator[bytes], Mapping[str, str]]]:
        """
        Returns:
            A tuple of bytes iterator and headers dict for back to client.
        """
        # cache file available, lookup the db for metadata
        meta_db_entry = await self._lru_helper.lookup_entry(cache_identifier)
        if not meta_db_entry:
            return

        # NOTE: db_entry.file_sha256 can be either
        #           1. valid sha256 value for corresponding plain uncompressed OTA file
        #           2. URL based sha256 value for corresponding requested URL
        # otaclient indicates that this cache entry is invalid, cleanup and exit
        cache_file = self._base_dir / cache_identifier
        if retry_cache:
            logger.debug(f"requested with retry_cache: {meta_db_entry=}..")
            await self._lru_helper.remove_entry(cache_identifier)
            cache_file.unlink(missing_ok=True)
            return

        # check if cache file exists
        if not cache_file.is_file():
            logger.warning(
                f"dangling cache entry found, remove db entry: {meta_db_entry}"
            )
            await self._lru_helper.remove_entry(cache_identifier)
            return

        # NOTE: we don't verify the cache here even cache is old, but let otaclient's hash verification
        #       do the job. If cache is invalid, otaclient will use CacheControlHeader's retry_cache
        #       directory to indicate invalid cache.
        return (
            read_file(cache_file, executor=self._executor),
            meta_db_entry.export_headers_to_client(),
        )

    async def _retrieve_file_by_external_cache(
        self, client_cache_policy: OTAFileCacheControl
    ) -> Optional[Tuple[AsyncIterator[bytes], Mapping[str, str]]]:
        # skip if not external cache or otaclient doesn't sent valid file_sha256
        if not self._external_cache or not client_cache_policy.file_sha256:
            return

        cache_identifier = client_cache_policy.file_sha256
        cache_file = self._external_cache / cache_identifier
        cache_file_zst = cache_file.with_suffix(
            f".{cfg.EXTERNAL_CACHE_STORAGE_COMPRESS_ALG}"
        )

        if cache_file_zst.is_file():
            return read_file(cache_file_zst, executor=self._executor), {
                HEADER_OTA_FILE_CACHE_CONTROL: OTAFileCacheControl.export_kwargs_as_header(
                    file_sha256=cache_identifier,
                    file_compression_alg=cfg.EXTERNAL_CACHE_STORAGE_COMPRESS_ALG,
                )
            }
        elif cache_file.is_file():
            return read_file(cache_file, executor=self._executor), {
                HEADER_OTA_FILE_CACHE_CONTROL: OTAFileCacheControl.export_kwargs_as_header(
                    file_sha256=cache_identifier
                )
            }

    # exposed API

    async def retrieve_file(
        self,
        raw_url: str,
        headers_from_client: Dict[str, str],
    ) -> Optional[Tuple[AsyncIterator[bytes], Mapping[str, str]]]:
        """Retrieve a file descriptor for the requested <raw_url>.

        This method retrieves a file descriptor for incoming client request.
        Upper uvicorn app can use this file descriptor to yield chunks of data,
        and send chunks to the on-calling ota_client.

        NOTE: use raw_url in all operations, except opening remote file.

        Args:
            raw_url: unquoted raw url received from uvicorn
            headers_from_client: headers come from client's request, which will be
                passthrough to upper otaproxy and/or remote OTA image server.

        Returns:
            A tuple contains an asyncio generator for upper server app to yield data chunks from
            and headers dict that should be sent back to client in resp.
        """
        if self._closed:
            raise BaseOTACacheError("ota cache pool is closed")

        cache_policy = OTAFileCacheControl.parse_header(
            headers_from_client.get(HEADER_OTA_FILE_CACHE_CONTROL, "")
        )
        if cache_policy.no_cache:
            logger.info(f"client indicates that do not cache for {raw_url=}")

        if not self._upper_proxy:
            headers_from_client.pop(HEADER_OTA_FILE_CACHE_CONTROL, None)

        # --- case 1: not using cache, directly download file --- #
        if (
            not self._cache_enabled  # ota_proxy is configured to not cache anything
            or cache_policy.no_cache  # ota_client send request with no_cache policy
            or not self._storage_below_hard_limit_event.is_set()  # disable cache if space hardlimit is reached
        ):
            logger.debug(
                f"not use cache({self._cache_enabled=}, {cache_policy=}, "
                f"{self._storage_below_hard_limit_event.is_set()=}): {raw_url=}"
            )
            return await self._retrieve_file_by_downloading(
                raw_url, headers=headers_from_client
            )

        # --- case 2: if externel cache source available, try to use it --- #
        # NOTE: if client requsts with retry_caching directive, it may indicate cache corrupted
        #       in external cache storage, in such case we should skip the use of external cache.
        if (
            self._external_cache
            and not cache_policy.retry_caching
            and (_res := await self._retrieve_file_by_external_cache(cache_policy))
        ):
            return _res

        # pre-calculated cache_identifier and corresponding compression_alg
        cache_identifier = cache_policy.file_sha256
        compression_alg = cache_policy.file_compression_alg
        if (
            not cache_identifier
        ):  # fallback to use URL based hash, and clear compression_alg
            cache_identifier = url_based_hash(raw_url)
            compression_alg = ""

        # --- case 3: try to use local cache --- #
        if _res := await self._retrieve_file_by_cache(
            cache_identifier, retry_cache=cache_policy.retry_caching
        ):
            return _res

        # --- case 4: no cache available, streaming remote file and cache --- #
        tracker, is_writer = await self._on_going_caching.get_tracker(
            cache_identifier,
            executor=self._executor,
            callback=self._commit_cache_callback,
            below_hard_limit_event=self._storage_below_hard_limit_event,
        )
        if is_writer:
            try:
                remote_fd, resp_headers = await self._retrieve_file_by_downloading(
                    raw_url, headers=headers_from_client
                )
            except Exception:
                await tracker.provider_on_failed()
                raise

            cache_meta = create_cachemeta_for_request(
                raw_url,
                cache_identifier,
                compression_alg,
                resp_headers_from_upper=resp_headers,
            )

            # start caching
            wrapped_fd = await cache_streaming(
                fd=remote_fd,
                meta=cache_meta,
                tracker=tracker,
            )
            return wrapped_fd, resp_headers

        else:
            stream_fd = await tracker.subscriber_subscribe_tracker()
            if stream_fd and tracker.meta:
                logger.debug(f"reader subscribe for {tracker.meta=}")
                return stream_fd, tracker.meta.export_headers_to_client()
