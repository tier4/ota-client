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
import aiofiles
import aiohttp
import bisect
import logging
import shutil
import time
import threading
import weakref
from concurrent.futures import Executor, ThreadPoolExecutor
from pathlib import Path
from os import urandom
from typing import (
    AsyncGenerator,
    AsyncIterator,
    Callable,
    Coroutine,
    Dict,
    Generic,
    List,
    MutableMapping,
    Optional,
    Tuple,
    TypeVar,
    Union,
)
from urllib.parse import SplitResult, quote, urlsplit

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
from .utils import (
    wait_with_backoff,
    AIOSHA256Hasher,
    read_file,
)

logger = logging.getLogger(__name__)


_WEAKREF = TypeVar("_WEAKREF")


class CacheTracker(Generic[_WEAKREF]):
    """A tracker for an ongoing cache entry.

    This tracker represents a temp cache entry under the <cache_dir>,
    and takes care of the life cycle of this temp cache entry. It implements
    the provider/subscriber model for cache writing and cache streaming to
    multiple clients.

    This entry will disappear automatically, ensured by gc and weakref
    when no strong reference to this tracker(provider finished and no subscribers
    attached to this tracker). When this tracker is garbage collected, the corresponding
    temp cache entry will also be removed automatically(ensure by registered finalizer).

    The typical usage of CacheTracker is to bind it to an inst of RemoteOTAFile, RemoteOTAFile
    will use this tracker to store and multi-streaming the cache entry.

    Attributes:
        fpath: the path to the temporary cache file.
        meta: an inst of CacheMeta for the remote OTA file that being cached.
        writer_ready: a property indicates whether the provider is
            ready to write and streaming data chunks.
        writer_finished: a property indicates whether the provider finished the caching.
        writer_failed: a property indicates whether provider fails to
            finish the caching.
    """

    READER_SUBSCRIBE_WAIT_PROVIDER_TIMEOUT = 2

    def __init__(
        self,
        fn: str,
        ref_holder: _WEAKREF,
        *,
        base_dir: Union[str, Path],
        executor: Executor,
    ):
        self.fpath = Path(base_dir) / fn
        self.meta = None
        self._writer_ready = asyncio.Event()
        self._writer_finished = asyncio.Event()
        self._writer_failed = asyncio.Event()
        self._ref = ref_holder
        self._subscriber_ref_holder: List[_WEAKREF] = []
        self._executor = executor

        self._bytes_written = 0
        self._cache_write_gen: Optional[AsyncGenerator[int, bytes]] = None

        # self-register the finalizer to this tracker
        weakref.finalize(
            self,
            self.finalizer,
            fpath=self.fpath,
        )

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

    def get_cache_write_gen(self) -> AsyncGenerator[int, bytes]:
        if not self._cache_write_gen:
            raise ValueError("being called before provider is ready")
        return self._cache_write_gen

    async def _provider_write_cache(
        self, *, storage_below_hard_limit: threading.Event
    ) -> AsyncGenerator[int, bytes]:
        """Provider writes data chunks from upper caller send() to tmp cache file.

        Args:
            storage_below_hard_limit: an inst of threading.Event that indicates
                whether the storage is enough.

        Raises:
            If storage hard limit is reached, the writing will be interrupted and
            a StorageReachHardLimit exception will be raised.
            Also any exception related to writing file will be directly propagated to
            the upper caller.
            The exception from upper caller via throw() will also be re-raised directly.
        """
        logger.debug(f"start to cache for {self.meta=}...")
        _sha256hash_f = AIOSHA256Hasher(executor=self._executor)
        async with aiofiles.open(self.fpath, "wb", executor=self._executor) as f:
            _written = 0
            while _data := (yield _written):
                if not storage_below_hard_limit.is_set():
                    logger.warning(f"reach storage hard limit, abort: {self.meta=}")
                    raise StorageReachHardLimit
                await _sha256hash_f.update(_data)
                _written = await f.write(_data)
                self._bytes_written += _written
        self.meta.size = self._bytes_written  # type: ignore
        self.meta.sha256hash = await _sha256hash_f.hexdigest()  # type: ignore
        logger.debug(
            "cache write finished, total bytes written"
            f"({self._bytes_written}) for {self.meta=}"
        )

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
        """Directly open the tmp cache entry and yield data chunks from it."""
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
                        "timeout getting more data, partial read detected"
                    )
                    logger.debug(_err_msg)
                    # signal streamer to stop streaming
                    raise CacheMultiStreamingFailed(_err_msg)

    # exposed API

    async def subscriber_read_cache(self) -> Optional[AsyncIterator[bytes]]:
        """Subscribe to an already finished cache file.

        Returns:
            If the cache is finished and valid, returns an async iterator
            that can be used to yield data chunks from.
        """
        if not self.is_cache_valid:
            return
        return self._read_cache()

    async def provider_start(
        self, meta: CacheMeta, *, storage_below_hard_limit: threading.Event
    ):
        """Register meta to the Tracker, create tmp cache entry and get ready.

        Check _provider_write_cache for more details.

        Args:
            meta: inst of CacheMeta for the requested file tracked by this tracker.
                This meta is created by open_remote() method.
            storage_below_hard_limit: an inst of threading.Event indicates whether the
                storage usage is below hard limit for allowing caching.
        """
        self.meta = meta
        self._cache_write_gen = self._provider_write_cache(
            storage_below_hard_limit=storage_below_hard_limit,
        )
        await self._cache_write_gen.asend(None)  # type: ignore
        self._writer_ready.set()

    async def provider_on_finished(self):
        if self._cache_write_gen:
            # gracefully stop the cache_write_gen
            try:
                await self._cache_write_gen.asend(b"")
            except StopAsyncIteration:
                pass
        self._writer_finished.set()
        try:
            # prevent future subscription, and let gc
            # collect this tracker along with the tmp
            del self._ref
        except AttributeError:
            pass

    async def provider_on_failed(self):
        self._writer_failed.set()
        self._writer_finished.set()
        # force stop the cache_write_gen generator if any
        if self._cache_write_gen:
            try:
                await self._cache_write_gen.athrow(CacheStreamingInterrupt)
            except (StopAsyncIteration, CacheStreamingInterrupt):
                logger.warning(f"interrupt writer coroutine for {self.meta=}")
        try:
            # prevent future subscription, and let gc
            # collect this tracker along with the tmp
            del self._ref
        except AttributeError:
            pass

    async def subscriber_subscribe_tracker(self) -> Optional[AsyncIterator[bytes]]:
        """Reader subscribe this tracker and get a file descriptor to get data chunks.

        Subscribe only succeeds when tracker is still on-going.
        """
        _wait_count = 0
        while not self._writer_ready.is_set():
            _wait_count += 1
            if self.writer_failed or not await wait_with_backoff(
                _wait_count,
                _backoff_factor=cfg.STREAMING_BACKOFF_FACTOR,
                _backoff_max=self.READER_SUBSCRIBE_WAIT_PROVIDER_TIMEOUT,
            ):
                # timeout waiting for provider to become ready
                return

        # subscribe by adding a new ref
        try:
            self._subscriber_ref_holder.append(self._ref)
            return self._subscribe_cache_streaming()
        except AttributeError:
            # just encount the end of writer caching, abort
            return


# a callback that register the cache entry indicates by input
# CacheMeta inst to the cache_db
_CACHE_ENTRY_REGISTER_CALLBACK = Callable[[CacheMeta], Coroutine[None, None, None]]


class _Weakref:
    pass


class CachingRegister:
    """A tracker register that manages cache trackers.

    For each requested URL for remote OTA file, there will be only one tracker.
    This first caller that requests to a URL will become the provider and create
    a new tracker for this URL. The later comes callers will become the subscriber
    to this tracker.
    """

    def __init__(self, base_dir: Union[str, Path]):
        self._base_dir = Path(base_dir)
        self._url_ref_dict: MutableMapping[
            str, _Weakref
        ] = weakref.WeakValueDictionary()
        self._ref_tracker_dict: MutableMapping[
            _Weakref, CacheTracker
        ] = weakref.WeakKeyDictionary()

    async def get_tracker(
        self, raw_url: str, *, executor: Executor
    ) -> Tuple[CacheTracker, bool]:
        """Get an inst of CacheTracker for the requested URL.

        Returns:
            An inst of tracker, and a bool indicates whether the caller is subscriber
                or provider.
        """
        _ref = self._url_ref_dict.setdefault(raw_url, (_new_ref := _Weakref()))
        # subscriber
        if (
            _tracker := self._ref_tracker_dict.get(_ref)
        ) and not _tracker.writer_failed:
            return _tracker, False

        # provider, or override a failed provider
        if _ref is not _new_ref:  # override a failed tracker
            self._url_ref_dict[raw_url] = _new_ref
            _ref = _new_ref

        _tracker = CacheTracker(
            f"{cfg.TMP_FILE_PREFIX}_{urandom(16).hex()}",
            _ref,
            base_dir=self._base_dir,
            executor=executor,
        )
        self._ref_tracker_dict[_ref] = _tracker
        return _tracker, True


class LRUCacheHelper:
    """A helper class that provides API for accessing/managing cache entries in ota cachedb.

    Serveral buckets are created according to predefined file size threshould.
    Each bucket will maintain the cache entries of that bucket's size definition,
    LRU is applied on per-bucket scale.

    NOTE: currently entry that has size larger than 512MiB or smaller that 1KiB will skip LRU rotate.
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
        entry.bucket_idx = bisect.bisect_right(self.BSIZE_LIST, entry.size) - 1
        entry.last_access = int(time.time())

        if (await self._db.insert_entry(entry)) != 1:
            logger.error(f"db: failed to add {entry=}")
            return False
        return True

    async def lookup_entry_by_url(self, url: str) -> Optional[CacheMeta]:
        return await self._db.lookup_entry(CacheMeta.url, url)

    async def remove_entry_by_url(self, url: str) -> bool:
        return (await self._db.remove_entries(CacheMeta.url, url)) > 0

    async def rotate_cache(self, size: int) -> Union[List[str], None]:
        """Wrapper method for calling the database LRU cache rotating method.

        Args:
            size int: the size of file that we want to reserve space for

        Returns:
            A list of hashes that needed to be cleaned, or None if cache rotation
                cannot be executed.
        """
        # NOTE: currently file size >= 512MiB or file size < 1KiB
        # will be saved without cache rotating.
        if size >= self.BSIZE_LIST[-1] or size < self.BSIZE_LIST[1]:
            return []

        _cur_bucket_idx = bisect.bisect_right(self.BSIZE_LIST, size) - 1
        _cur_bucket_size = self.BSIZE_LIST[_cur_bucket_idx]

        # first check the upper bucket
        _next_idx = _cur_bucket_idx + 1
        for _bucket_size in self.BSIZE_LIST[_next_idx:]:
            if res := await self._db.rotate_cache(
                _next_idx, self.BSIZE_DICT[_bucket_size]
            ):
                return res
            _next_idx += 1

        # if cannot find one entry at any upper bucket, check current bucket
        return await self._db.rotate_cache(
            _cur_bucket_idx, self.BSIZE_DICT[_cur_bucket_size]
        )


class RemoteOTAFile:
    """File descriptor that represents an ongoing cache entry.

    Instance of RemoteOTAFile wraps a CacheMeta and a CacheTracker for specific URL,
    and a opened remote file descriptor that can be used to yield data chunks from.

    Each file will first be cached under <base_dir> with a tmp file name,
    successfully cached entry will be rename with its sha256hash and comitted
    to the cache_db for future use by executing the <commit_cache_callback> callable.

    Attributes:
        fd: opened connection to a remote file.
        meta: meta data of the resource indicated by URL.
        below_hard_limit_event: a Event instance that can be used to check whether
            local storage space is enough for caching. If reaches hard limit, the
            caching will be interrupted.
        tracker: an inst of ongoing cache tracker bound to this remote ota file.
        base_dir: the location for cached files and tmp cache files.
        executor: an inst of ThreadPoolExecutor for dispatching cache entry commit.
        commit_cache_callback: a callback to commit the cached file entry to the cache_db.
    """

    def __init__(
        self,
        fd: AsyncIterator[bytes],
        meta: CacheMeta,
        *,
        tracker: CacheTracker,
        below_hard_limit_event: threading.Event,
        base_dir: Union[str, Path],
        commit_cache_callback: _CACHE_ENTRY_REGISTER_CALLBACK,
    ):
        self._base_dir = Path(base_dir)
        self._storage_below_hard_limit = below_hard_limit_event
        self._fd = fd
        # NOTE: the hash and size in the meta are not set yet
        # NOTE 2: store unquoted URL in database
        self.meta = meta
        self._tracker = tracker  # bound tracker
        self._cache_commit_cb = commit_cache_callback

    async def _finalize_caching(self):
        """Commit cache entry to db and rename tmp cached file with sha256hash
        to fialize the caching."""
        await self._cache_commit_cb(self.meta)
        # if the file with the same sha256has is already presented, skip the hardlink
        if not (self._base_dir / self.meta.sha256hash).is_file():
            self._tracker.fpath.link_to(self._base_dir / self.meta.sha256hash)

    async def _cache_streamer(self) -> AsyncIterator[bytes]:
        """For caller(server App) to yield data chunks from.

        This method yields data chunks from self._fd(opened remote connection),
        and then streams data chunks to uvicorn app and cache writing generator,
        similar to the linux command tee does.

        Returns:
            An AsyncIterator for upper caller to yield data chunks from.
        """
        try:
            _cache_write_gen = self._tracker.get_cache_write_gen()
            async for chunk in self._fd:
                if not chunk:  # skip if empty chunk is read
                    continue
                # to caching generator
                if not self._tracker.writer_finished:
                    try:
                        await _cache_write_gen.asend(chunk)
                    except Exception as e:
                        await self._tracker.provider_on_failed()  # signal tracker
                        logger.error(
                            f"cache write coroutine failed for {self.meta=}, abort caching: {e!r}"
                        )
                # to uvicorn thread
                yield chunk
            await self._tracker.provider_on_finished()  # signal tracker
            # dispatch cache commit, no need to check the result of committing
            asyncio.create_task(self._finalize_caching())
        except Exception as e:
            logger.exception(f"cache tee failed for {self._tracker.meta=}")
            # if any exception happens, signal the tracker
            await self._tracker.provider_on_failed()
            raise CacheStreamingFailed from e

    # exposed API

    async def start_cache_streaming(self) -> Tuple[AsyncIterator[bytes], CacheMeta]:
        """A wrapper method that create and start the cache writing generator.

        Returns:
            A tuple of an AsyncIterator and CacheMeta for this RemoteOTAFile.
        """
        # bind the updated meta to tracker,
        # and make tracker ready
        await self._tracker.provider_start(
            self.meta,
            storage_below_hard_limit=self._storage_below_hard_limit,
        )
        return (self._cache_streamer(), self.meta)


async def open_remote(
    url: str,
    raw_url: str,
    *,
    cookies: Dict[str, str],
    headers: Dict[str, str],
    session: aiohttp.ClientSession,
    upper_proxy: str = "",
) -> Tuple[AsyncIterator[bytes], CacheMeta]:
    """Open a file descriptor to remote resource.

    Args:
        url: quoted url.
        raw_url: unquoted url, used in CacheMeta.
        cookies: cookies that client passes in the request.
        extra_headers: other headers we need to pass to the remote
            from the original request.
        session: an inst of aiohttp.ClientSession used for opening
            remote connection.
        upper_proxy: if chained proxy is used.

    Returns:
        An AsyncIterator that can yield data chunks from, and an inst
            of CacheMeta for the requested url.

    Raises:
        Any exceptions during aiohttp connecting to the remote.
    """
    cache_meta = CacheMeta(url=raw_url)

    async def _inner() -> AsyncIterator[bytes]:
        async with session.get(
            url,
            proxy=upper_proxy,
            cookies=cookies,
            headers=headers,
        ) as response:
            # assembling output cachemeta
            # NOTE: output cachemeta doesn't have hash, size, bucket, last_access defined set yet
            # NOTE.2: store the original unquoted url into the CacheMeta
            # NOTE.3: hash, and size will be assigned at background_write_cache method
            # NOTE.4: bucket and last_access will be assigned at commit_entry method
            cache_meta.content_encoding = response.headers.get("content-encoding", "")
            cache_meta.content_type = response.headers.get(
                "content-type", "application/octet-stream"
            )
            # open the connection and update the CacheMeta
            yield b""
            async for data, _ in response.content.iter_chunks():
                if data:  # only yield non-empty data chunk
                    yield data

    # open remote connection
    await (_remote_fd := _inner()).__anext__()
    return _remote_fd, cache_meta


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

        # NOTE: we configure aiohttp to not decompress the contents,
        # we cache the contents as its original form, and send
        # to the client with proper headers to indicate the client to
        # compress the payload by their own
        # NOTE 2: disable aiohttp default timeout(5mins)
        # this timeout will be applied to the whole request, including downloading,
        # preventing large files to be downloaded.
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
                current_used_p = disk_usage.used / disk_usage.total * 100
                if current_used_p < cfg.DISK_USE_LIMIT_SOFT_P:
                    logger.debug(
                        f"storage usage below soft limit: {current_used_p:.2f}%"
                    )
                    # below soft limit, normal caching mode
                    self._storage_below_soft_limit_event.set()
                    self._storage_below_hard_limit_event.set()
                elif (
                    current_used_p >= cfg.DISK_USE_LIMIT_SOFT_P
                    and current_used_p < cfg.DISK_USE_LIMIT_HARD_P
                ):
                    logger.debug(
                        f"storage usage below hard limit: {current_used_p:.2f}%"
                    )
                    # reach soft limit but not reach hard limit
                    # space reservation will be triggered after new file cached
                    self._storage_below_soft_limit_event.clear()
                    self._storage_below_hard_limit_event.set()
                else:
                    logger.debug(
                        f"storage usage reach hard limit: {current_used_p:.2f}%"
                    )
                    # reach hard limit
                    # totally disable caching
                    self._storage_below_soft_limit_event.clear()
                    self._storage_below_hard_limit_event.clear()
            except FileNotFoundError:
                logger.error(
                    "background free space check interrupted as cache folder disappeared"
                )
                self._storage_below_soft_limit_event.clear()
                self._storage_below_hard_limit_event.clear()
                break
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
        if _hashes:
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
                if await self._reserve_space(meta.size):
                    if not await self._lru_helper.commit_entry(meta):
                        logger.debug(f"failed to commit cache for {meta.url=}")
                else:
                    # case 2: cache successful, but reserving space failed,
                    # NOTE(20221018): let gc to remove the tmp file
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
        self, raw_url: str, *, cookies, extra_headers
    ) -> Tuple[AsyncIterator[bytes], CacheMeta]:
        # NOTE: store unquoted url in database
        return await open_remote(
            url=self._process_raw_url(raw_url),
            raw_url=raw_url,
            cookies=cookies,
            headers=extra_headers,
            session=self._session,
            upper_proxy=self._upper_proxy,
        )

    async def _retrieve_file_by_cache(
        self, raw_url: str, *, retry_cache: bool
    ) -> Optional[Tuple[AsyncIterator[bytes], CacheMeta]]:
        meta_db_entry = await self._lru_helper.lookup_entry_by_url(raw_url)
        if not meta_db_entry:
            return

        cache_file = self._base_dir / meta_db_entry.sha256hash
        # otaclient indicates that this cache entry is invalid, cleanup and exit
        if retry_cache:
            logger.debug(f"requested with retry_cache: {meta_db_entry=}..")
            await self._lru_helper.remove_entry_by_url(raw_url)
            cache_file.unlink(missing_ok=True)
            return

        # check if cache file exists
        if not cache_file.is_file():
            logger.warning(
                f"dangling cache entry found, remove db entry: {meta_db_entry}"
            )
            await self._lru_helper.remove_entry_by_url(raw_url)
            return

        # NOTE: we don't verify the cache here even cache is old, but let otaclient's hash verification
        #       do the job. If cache is invalid, otaclient will use CacheControlHeader's retry_cache
        #       directory to indicate invalid cache.
        return read_file(cache_file, executor=self._executor), meta_db_entry

    async def _retrieve_file_by_cache_streaming(
        self, raw_url: str, *, cookies, extra_headers
    ) -> Optional[Tuple[AsyncIterator[bytes], CacheMeta]]:
        tracker, is_writer = await self._on_going_caching.get_tracker(
            raw_url, executor=self._executor
        )
        if is_writer:
            try:
                _remote_fd, _meta = await self._retrieve_file_by_downloading(
                    raw_url, cookies=cookies, extra_headers=extra_headers
                )
            except Exception:
                await tracker.provider_on_failed()
                raise

            return await RemoteOTAFile(
                fd=_remote_fd,
                meta=_meta,
                tracker=tracker,
                below_hard_limit_event=self._storage_below_hard_limit_event,
                base_dir=self._base_dir,
                commit_cache_callback=self._commit_cache_callback,
            ).start_cache_streaming()
        else:
            # get the ongoing cache file descriptor if available
            stream_fd = await tracker.subscriber_subscribe_tracker()
            if stream_fd and tracker.meta:
                logger.debug(f"reader subscribe for {tracker.meta=}")
                return stream_fd, tracker.meta

            # if no stream_fd is available(caching is finished), try to directly use already finished tmp cache
            cache_file_fd = await tracker.subscriber_read_cache()
            if cache_file_fd and tracker.meta:
                logger.debug(f"reader directly use cached file for {tracker.meta=}")
                return cache_file_fd, tracker.meta

    # exposed API

    async def retrieve_file(
        self,
        raw_url: str,
        /,
        cookies: Dict[str, str],
        extra_headers: Dict[str, str],
        cache_control_policies: OTAFileCacheControl,
    ) -> Optional[Tuple[AsyncIterator[bytes], CacheMeta]]:
        """Retrieve a file descriptor for the requested <raw_url>.

        This method retrieves a file descriptor for incoming client request.
        Upper uvicorn app can use this file descriptor to yield chunks of data,
        and send chunks to the on-calling ota_client.

        NOTE: use raw_url in all operations, except opening remote file.

        Args:
            raw_url: unquoted raw url received from uvicorn
            cookies: cookies in the incoming client request.
            extra_headers: headers in the incoming client request.
                Currently Cookies and Authorization headers are used.
            cache_control_policies: OTA-FILE-CACHE-CONTROL headers that
                controls how ota-proxy should handle the request.

        Returns:
            A tuple contains an asyncio generator for upper server app to yield data chunks from
            and an inst of CacheMeta representing the requested URL.
        """
        if self._closed:
            raise BaseOTACacheError("ota cache pool is closed")

        # --- parsing cache control headers --- #
        # default cache control policy:
        retry_cache, use_cache = False, True
        # parse input policies
        if cache_control_policies.retry_caching:
            retry_cache = True
            logger.warning(f"client indicates that cache for {raw_url=} is invalid")
        if cache_control_policies.no_cache:
            logger.info(f"client indicates that do not cache for {raw_url=}")
            use_cache = False
        # if there is no upper_ota_proxy, trim the custom headers away
        if self._enable_https:
            extra_headers.pop(OTAFileCacheControl.HEADER_LOWERCASE, None)

        # --- case 1: not using cache, directly download file --- #
        if (
            not self._cache_enabled  # ota_proxy is configured to not cache anything
            or not use_cache  # ota_client send request with no_cache policy
            or not self._storage_below_hard_limit_event.is_set()  # disable cache if space hardlimit is reached
        ):
            logger.debug(
                f"not use cache({self._cache_enabled=}, {use_cache=}, "
                f"{self._storage_below_hard_limit_event.is_set()=}): {raw_url=}"
            )
            return await self._retrieve_file_by_downloading(
                raw_url, cookies=cookies, extra_headers=extra_headers
            )

        # --- case 2: try to use cache --- #
        if _res := await self._retrieve_file_by_cache(raw_url, retry_cache=retry_cache):
            return _res

        # --- case 3: no cache available, streaming remote file and cache --- #
        if _res := await self._retrieve_file_by_cache_streaming(
            raw_url, cookies=cookies, extra_headers=extra_headers
        ):
            logger.debug(f"no cache entry for {raw_url=}, start caching")
            return _res

        # --- case 4: failed to handle request--- #
        # NOTE: this is basically caused by network, so return None, let otaproxy
        #       interrupt the connection and let otaclient retry again
        logger.warning(
            "failed to handle request(might caused by "
            f"network interruption or space limitation): {raw_url=}"
        )
        return None
