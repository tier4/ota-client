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
from datetime import datetime
from functools import partial
from hashlib import sha256
from os import urandom
from pathlib import Path
from queue import Queue
from typing import (
    TYPE_CHECKING,
    AsyncIterator,
    Callable,
    Dict,
    Generic,
    List,
    Optional,
    Set,
    Tuple,
    TypeVar,
    Union,
)
from urllib.parse import SplitResult, quote, urlsplit

from .cache_control import OTAFileCacheControl
from .db import CacheMeta, OTACacheDB, OTACacheDBProxy
from .errors import BaseOTACacheError, CacheStreamingFailed, CacheMultiStreamingFailed
from .config import config as cfg

if TYPE_CHECKING:
    import multiprocessing

logger = logging.getLogger(__name__)


def get_backoff(n: int, factor: float, _max: float) -> float:
    return min(_max, factor * (2 ** (n - 1)))


_WEAKREF = TypeVar("_WEAKREF")


class OngoingCacheTracker(Generic[_WEAKREF]):
    """A tracker for an on-going cache entry.

    A tracker represents a temp cache entry under the <cache_dir>.
    This entry will disappear automatically when writer finished
    caching and commit the cache to the database, also the temp cache entry
    will be cleaned up.
    """

    READER_SUBSCRIBE_PULLING_INTERVAL = 1

    def __init__(self, fn: str, ref_holder: _WEAKREF):
        self.fn = fn
        self.meta = None
        self._writer_ready = asyncio.Event()
        self._cache_done = threading.Event()
        self._is_failed = threading.Event()
        self._writer_ref_holder = ref_holder
        self._subscriber_ref_holder: List[_WEAKREF] = []

    @property
    def is_failed(self) -> bool:
        return self._is_failed.is_set()

    @property
    def cache_done(self) -> bool:
        return self._cache_done.is_set()

    async def writer_on_ready(self, meta: CacheMeta):
        """Register meta to the Tracker and get ready."""
        self.meta = meta
        self._writer_ready.set()

    async def reader_subscribe(self) -> bool:
        """Reader subscribe this tracker.

        Subscribe only succeeds when tracker is still on-going.
        """
        while not self._writer_ready.is_set():
            if self._is_failed.is_set():
                return False
            await asyncio.sleep(self.READER_SUBSCRIBE_PULLING_INTERVAL)
        # subscribe by adding a new ref
        try:
            self._subscriber_ref_holder.append(self._writer_ref_holder)
            return True
        except AttributeError:
            # just encount the end of writer caching, abort
            return False

    def reader_on_done(self):
        try:
            self._subscriber_ref_holder.pop()
        except IndexError:
            pass

    def writer_on_done(self, success: bool):
        """Writer calls this method when caching is finished/interrupted.

        NOTE: must ensure cache entry is committed into the database
        before calling this function!
        NOTE 2: this method is cross-thread method

        Arg:
            success: indicates whether the caching is successful or not.
        """
        if not success:
            self._is_failed.set()
        self._cache_done.set()
        try:
            del self._writer_ref_holder
        except AttributeError:
            pass

    def get_meta_on_success(self) -> Optional[CacheMeta]:
        """Get the cache entry meta associated with this tracker.

        NOTE: only return meta on a successful cache.
        """
        if self.cache_done and not self.is_failed:
            return self.meta


_CACHE_ENTRY_REGISTER_CALLBACK = Callable[[OngoingCacheTracker], None]


class OngoingCachingRegister:
    """A tracker register class that provides cache streaming
        on same requested file from multiple caller.

    This tracker is implemented based on provider/subscriber model,
        with weakref to automatically cleanup finished sessions.
    """

    def __init__(self, base_dir: Union[str, Path]):
        self._base_dir = Path(base_dir)
        self._lock = threading.Lock()
        self._url_ref_dict: Dict[str, asyncio.Event] = weakref.WeakValueDictionary()  # type: ignore
        self._ref_tracker_dict: Dict[asyncio.Event, OngoingCacheTracker] = weakref.WeakKeyDictionary()  # type: ignore

    def _finalizer(self, fn: str):
        # cleanup(unlink) the tmp file when corresponding tracker is gced.
        (self._base_dir / fn).unlink(missing_ok=True)

    async def get_or_subscribe_tracker(
        self, url: str
    ) -> Tuple[Optional[OngoingCacheTracker], bool]:
        # subscriber
        if _ref := self._url_ref_dict.get(url):
            await _ref.wait()  # wait for writer tracker to be initialized
            if await (_tracker := self._ref_tracker_dict[_ref]).reader_subscribe():
                return _tracker, False
            return None, False
        # provider
        else:
            _tmp_fn = f"tmp_{urandom(16).hex()}"
            self._url_ref_dict[url] = (_ref := asyncio.Event())
            self._ref_tracker_dict[_ref] = (
                _tracker := OngoingCacheTracker(_tmp_fn, _ref)
            )
            # cleanup tmp file when _tracker is gc.
            weakref.finalize(_tracker, self._finalizer, _tmp_fn)

            _ref.set()
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
        self._db = OTACacheDBProxy(db_f)
        self._closed = False

    def close(self):
        if not self._closed:
            self._db.close()

    def commit_entry(self, entry: CacheMeta) -> bool:
        """Commit cache entry meta to the database."""
        # populate bucket and last_access column
        entry.bucket_idx = bisect.bisect_right(self.BSIZE_LIST, entry.size) - 1
        entry.last_access = int(datetime.now().timestamp())

        if self._db.insert_entry(entry) != 1:
            logger.warning(f"db: failed to add {entry=}")
            return False
        else:
            logger.debug(f"db: commit {entry=} successfully")
            return True

    async def lookup_entry_by_url(
        self, url: str, *, executor: Executor
    ) -> Optional[CacheMeta]:
        return await asyncio.get_running_loop().run_in_executor(
            executor,
            self._db.lookup_entry,
            CacheMeta.url,
            url,
        )

    async def remove_entry_by_url(self, url: str, *, executor: Executor) -> bool:
        return (
            await asyncio.get_running_loop().run_in_executor(
                executor, self._db.remove_entries, CacheMeta.url, url
            )
            > 0
        )

    def rotate_cache(self, size: int) -> Union[List[str], None]:
        """Wrapper method for calling the database LRU cache rotating method.

        Args:
            size int: the size of file that we want to reserve space for

        Returns:
            A list of hashes that needed to be cleaned, or None cache rotation request
                cannot be satisfied.
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
            if res := self._db.rotate_cache(_next_idx, self.BSIZE_DICT[_bucket_size]):
                return res
            _next_idx += 1

        # if cannot find one entry at any upper bucket, check current bucket
        return self._db.rotate_cache(_cur_bucket_idx, self.BSIZE_DICT[_cur_bucket_size])


class RemoteOTAFile:
    """File descriptor for data streaming.

    Instance of OTAFile wraps meta data for specific URL,
    along with a file descriptor(an AsyncIterator) that can be used
    to yield chunks of data from.
    Instance of OTAFile is requested by the upper uvicorn app,
    and being created and passed to app by OTACache instance.
    Check OTACache.retreive_file for details.

    Attributes:
        meta CacheMeta: meta data of the resource indicated by URL.
        below_hard_limit_event Event: a Event instance that can be used to check whether
            local storage space is enough for caching.
    """

    def __init__(
        self,
        url: str,
        raw_url: str,
        *,
        tracker: OngoingCacheTracker,
        below_hard_limit_event: threading.Event,
        base_dir: Union[str, Path],
    ):
        self._base_dir = Path(base_dir)
        self._storage_below_hard_limit = below_hard_limit_event
        # NOTE: the hash and size in the meta are not set yet
        # NOTE 2: store unquoted URL in database
        self.meta = CacheMeta(url=raw_url)
        self.url = url  # proccessed url
        self._tracker = tracker

        # life cycle
        self._fd_eof = threading.Event()
        self._queue = Queue()

    def _background_write_cache(
        self,
        commit_cache_entry_callback: _CACHE_ENTRY_REGISTER_CALLBACK,
    ):
        """Caching files on to the local disk in the background thread.

        When OTAFile instance initialized and launched,
        a queue is opened between this method and the wrapped fd generator.
        This method will receive data chunks from the queue, calculate the hash,
        and cache the data chunks onto the disk.
        Backoff retry is applied here to allow delays of data chunks arrived in the queue.

        This method should be called when the OTAFile instance is created.
        A callback method should be assigned when this method is called.

        Args:
            tracker: an OngoingCacheTracker to sync status with subscriber.
            callback: a callback function to do post-caching jobs.
        """
        # populate these 2 properties of CacheMeta in this method
        _hash, _size = "", 0
        _sha256hash_f = sha256()
        try:
            logger.debug(f"start to cache for {self.meta.url}...")
            temp_fpath = self._base_dir / self._tracker.fn

            with open(temp_fpath, "wb") as dst_f:
                err_count = 0
                while not self._fd_eof.is_set() or not self._queue.empty():
                    # first check the tracker, the streamer might already failed
                    if self._tracker.is_failed:
                        raise ValueError("streamer failed")

                    # reach storage hard limit, abort caching
                    if not self._storage_below_hard_limit.is_set():
                        _err_msg = f"not enough free space during caching url={self.meta.url}, abort"
                        logger.debug(_err_msg)
                        # and then breakout the loop
                        raise ValueError(_err_msg)

                    # get data chunk from stream
                    _timeout = get_backoff(
                        err_count,
                        cfg.STREAMING_BACKOFF_FACTOR,
                        cfg.STREAMING_TIMEOUT,
                    )
                    try:
                        if data := self._queue.get(timeout=_timeout):
                            _sha256hash_f.update(data)
                            _size += dst_f.write(data)
                        err_count = 0
                    except Exception:  # timeout waiting for data chunk
                        err_count += 1
                        if _timeout >= cfg.STREAMING_TIMEOUT:
                            # abort caching due to potential dead streaming coro
                            _err_msg = f"failed to cache {self.meta.url}: timeout getting data from queue"
                            logger.debug(_err_msg)
                            # signal streamer to stop streaming
                            raise ValueError(_err_msg)

            # cache successfully
            # update meta
            _hash = _sha256hash_f.hexdigest()
            self.meta.sha256hash = _hash
            self.meta.size = _size
            # commit cache entry
            self._tracker.writer_on_done(success=True)
            commit_cache_entry_callback(self._tracker)
            # for 0 size file, register the entry only
            # but if the 0 size file doesn't exist, create one
            if _size > 0 or not (self._base_dir / _hash).is_file():
                logger.debug(f"successfully cached {self.meta.url}")
                # NOTE: hardlink to new file name,
                # tmp file cleanup will be conducted by CachingTracker
                try:
                    temp_fpath.link_to(self._base_dir / _hash)
                except FileExistsError:
                    # this might caused by entry with same file contents
                    logger.debug(f"entry {_hash} existed, ignored")

        except Exception as e:
            logger.debug(f"failed on writing cache for {self._tracker.fn}: {e!r}")
            self._tracker.writer_on_done(success=False)

    async def _stream(self, fd) -> AsyncIterator[bytes]:
        """For caller(server App) to yield data chunks from.

        This method yields data chunks from selves' file descriptor,
        and then streams data chunks to upper caller and caching thread(if cache is enabled)
        similar to the linux command tee does.

        Returns:
            An AsyncIterator for upper caller to yield data chunks from.
        """
        try:
            async for chunk in fd:
                # to caching thread
                # stop teeing data chunk to caching thread
                # if caching thread indicates so
                if not self._tracker.is_failed:
                    self._queue.put_nowait(chunk)
                # to uvicorn thread
                yield chunk
        except Exception as e:
            # if any exception happens, signal the caching thread
            self._tracker.writer_on_done(success=False)
            logger.exception("cache tee failed")
            raise CacheStreamingFailed from e
        finally:
            # signal that the file descriptor is exhausted/interrupted
            self._fd_eof.set()

    async def open_remote(
        self,
        commit_cache_callback: _CACHE_ENTRY_REGISTER_CALLBACK,
        *,
        cookies: Dict[str, str],
        extra_headers: Dict[str, str],
        session: aiohttp.ClientSession,
        upper_proxy: str,
        executor: Executor,
    ) -> Optional[Tuple[AsyncIterator[bytes], CacheMeta]]:
        _remote_fd = _FileDescriptorHelper.open_remote(
            self.url,
            self.meta,  # updated when remote response is received
            cookies,
            extra_headers,
            session=session,
            upper_proxy=upper_proxy,
        )
        try:
            await _remote_fd.__anext__()  # open the remote connection
        except Exception as e:  # connection failed
            self._fd_eof.set()
            logger.error(f"remote connection to {self.url} failed: {e!r}")
            self._tracker.writer_on_done(success=False)
            raise

        await self._tracker.writer_on_ready(self.meta)
        # dispatch local cache storing to thread pool
        executor.submit(
            self._background_write_cache,
            commit_cache_callback,
        )
        return self._stream(_remote_fd), self.meta


class OTACacheScrubHelper:
    """Helper to scrub ota caches."""

    def __init__(self, db_file: Union[str, Path], base_dir: Union[str, Path]):
        self._db = OTACacheDB(db_file)
        self._db_file = Path(db_file)
        self._base_dir = Path(base_dir)
        self._excutor = ThreadPoolExecutor()

    @staticmethod
    def _check_entry(base_dir: Path, meta: CacheMeta) -> Tuple[CacheMeta, bool]:
        f = base_dir / meta.sha256hash
        if f.is_file():
            hash_f = sha256()
            # calculate file's hash and check against meta
            with open(f, "rb") as _f:
                while data := _f.read(cfg.CHUNK_SIZE):
                    hash_f.update(data)
            if hash_f.hexdigest() == meta.sha256hash:
                return meta, True

        # check failed, try to remove the cache entry
        f.unlink(missing_ok=True)
        return meta, False

    def scrub_cache(self):
        """Main entry for scrubbing cache folder.

        OTACacheScrubHelper instance will close itself after finishing scrubing.
        """
        logger.info("start to scrub the cache entries...")
        try:
            dangling_db_entry = []
            # NOTE: pre-add db related files into the set
            # to prevent db related files being deleted
            # NOTE 2: cache_db related files: <cache_db>, <cache_db>-shm, <cache_db>-wal, <cache>-journal
            db_file = self._db_file.name
            valid_cache_entry = {
                db_file,
                f"{db_file}-shm",
                f"{db_file}-wal",
                f"{db_file}-journal",
            }
            res_list = self._excutor.map(
                partial(self._check_entry, self._base_dir),
                self._db.lookup_all(),
                chunksize=128,
            )

            for meta, valid in res_list:
                if not valid:
                    logger.debug(f"invalid db entry found: {meta.url}")
                    dangling_db_entry.append(meta.url)
                else:
                    valid_cache_entry.add(meta.sha256hash)

            # delete the invalid entry from the database
            self._db.remove_entries(CacheMeta.url, *dangling_db_entry)

            # loop over all files under cache folder,
            # if entry's hash is not presented in the valid_cache_entry set,
            # we treat it as dangling cache entry and delete it
            for entry in self._base_dir.glob("*"):
                if entry.name not in valid_cache_entry:
                    logger.debug(f"dangling cache entry found: {entry.name}")
                    f = self._base_dir / entry.name
                    f.unlink(missing_ok=True)

            logger.info("cache scrub finished")
        except Exception as e:
            logger.error(f"failed to finish scrub cache folder: {e!r}")
        finally:
            self._db.close()
            self._excutor.shutdown(wait=True)


class _FileDescriptorHelper:
    CHUNK_SIZE = cfg.CHUNK_SIZE

    @classmethod
    async def open_remote(
        cls,
        url: str,
        cache_meta: CacheMeta,
        cookies: Dict[str, str],
        extra_headers: Dict[str, str],
        *,
        session: aiohttp.ClientSession,
        upper_proxy: str = "",
    ) -> AsyncIterator[bytes]:
        """Open a file descriptor to remote resource.

        Args:
            url: quoted url
            raw_url: unquoted url
            cookies: cookies that client passes in the request
            extra_headers: other headers we need to pass to the remote
                from the original request

        Returns:
            An AsyncGenerator that can yield data chunks from.

        Raises:
            Any exceptions that caused by connecting to the remote.
        """
        async with session.get(
            url,
            proxy=upper_proxy,
            cookies=cookies,
            headers=extra_headers,
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
                yield data

    @staticmethod
    async def open_cached_file(
        fpath: Union[str, Path],
        *,
        executor: Executor,
    ) -> AsyncIterator[bytes]:
        async with aiofiles.open(fpath, "rb", executor=executor) as f:
            while data := await f.read(cfg.CHUNK_SIZE):
                yield data

    @classmethod
    async def open_cache_stream(
        cls,
        tracker: OngoingCacheTracker,
        *,
        base_dir: Union[str, Path],
        executor: Executor,
    ) -> AsyncIterator[bytes]:
        _bytes_streamed = 0
        try:
            async with aiofiles.open(
                Path(base_dir) / tracker.fn, "rb", executor=executor
            ) as f:
                retry_count = 0
                while True:
                    ### get data chunk, yield it ###
                    if data := await f.read(cls.CHUNK_SIZE):
                        retry_count = 0
                        _bytes_streamed += len(data)
                        yield data
                        continue

                    ### no new data chunk is received ###
                    if tracker.cache_done:
                        if (_meta := tracker.get_meta_on_success()) is None:
                            raise ValueError(
                                f"writer for {tracker.fn=}, {tracker.meta=} failed, abort"
                            )
                        # check already read bytes, if it meets the meta.size,
                        # then cache stream finished, otherwise we should keep pulling
                        if not tracker.is_failed and _bytes_streamed == _meta.size:
                            return

                    ## writer blocked, retry ##
                    _wait = get_backoff(
                        retry_count,
                        cfg.STREAMING_BACKOFF_FACTOR,
                        cfg.STREAMING_TIMEOUT,
                    )
                    if _wait < cfg.STREAMING_TIMEOUT:
                        await asyncio.sleep(_wait)
                        retry_count += 1
                    else:
                        # timeout on waiting data from writer
                        raise TimeoutError(
                            f"timeout waiting data from writer: {tracker.fn}"
                        )
        except Exception as e:
            raise CacheMultiStreamingFailed(f"{e!r}")
        finally:
            tracker.reader_on_done()


class OTACache:
    """Maintain caches for ota update.

    Instance of this class handles the request from the upper caller,
    proxying the requests to the remote ota files server, caching the ota files
    and streaming data back to the upper caller. If cache is available for specific URL,
    it will stream the data from local caches.

    It wraps local cache(by open cache file) or remote resourecs(by open connection to the remote)
    into a file descriptor(an instance of OTAFile), the upper caller(instance of server_app)
    then can yield data chunks from the file descriptor and stream data chunks back to ota_client.

    Attributes:
        upper_proxy str: the upper proxy that ota_cache uses to send out request, default is None
        cache_enabled bool: when set to False, ota_cache will only relay requested data, default is False.
        enable_https bool: whether the ota_cache should send out the requests with HTTPS,
            default is False. NOTE: scheme change is applied unconditionally.
        init_cache bool: whether to clear the existed cache, default is True.
        scrub_cache_event: an multiprocessing.Event that sync status with the ota-client.
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
        scrub_cache_event: "Optional[multiprocessing.Event]" = None,  # type: ignore
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

        self._scrub_cache_event = scrub_cache_event

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
            total=None, sock_read=cfg.STREAMING_WAIT_FOR_FIRST_BYTE
        )
        self._session = aiohttp.ClientSession(
            auto_decompress=False, raise_for_status=True, timeout=timeout
        )

        if self._cache_enabled:
            # prepare cache dir
            if self._init_cache:
                shutil.rmtree(str(self._base_dir), ignore_errors=True)
                # if init, we also have to set the scrub_finished_event
                self._base_dir.mkdir(exist_ok=True, parents=True)
                # init only
                _init_only = OTACacheDB(self._db_file, init=True)
                _init_only.close()
            else:
                # scrub the cache folder
                _scrub_cache = OTACacheScrubHelper(
                    db_file=self._db_file, base_dir=self._base_dir
                )
                _scrub_cache.scrub_cache()

            # dispatch a background task to pulling the disk usage info
            self._executor.submit(self._background_check_free_space)

            # init cache helper
            self._lru_helper = LRUCacheHelper(self._db_file)
            self._on_going_caching = OngoingCachingRegister(self._base_dir)

            if self._upper_proxy:
                # if upper proxy presented, force disable https
                self._enable_https = False

        # set scrub_cache_event after init/scrub cache to signal the ota-client
        # TODO: passthrough the exception to the ota_client process
        #   if the init/scrub failed
        if self._scrub_cache_event:
            self._scrub_cache_event.set()

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
                    "background free space check failed as cache folder disappeared"
                )
                self._storage_below_soft_limit_event.clear()
                self._storage_below_hard_limit_event.clear()

            time.sleep(cfg.DISK_USE_PULL_INTERVAL)

    def _cache_entries_cleanup(self, entry_hashes: List[str]):
        """Cleanup entries indicated by entry_hashes list."""
        for entry_hash in entry_hashes:
            # remove cache entry
            f = self._base_dir / entry_hash
            f.unlink(missing_ok=True)

    def _reserve_space(self, size: int) -> bool:
        """A helper that calls lru_helper's rotate_cache method.

        Args:
            size: the size of the target that we need to reserve space for

        Returns:
            A bool indicates whether the space reserving is successful or not.
        """
        _hashes = self._lru_helper.rotate_cache(size)
        if _hashes:
            logger.debug(
                f"rotate on bucket({size=}), num of entries to be cleaned {len(_hashes)=}"
            )
            self._executor.submit(self._cache_entries_cleanup, _hashes)
            return True
        else:
            logger.debug(f"rotate on bucket({size=}) failed, no enough entries")
            return False

    def _register_cache_callback(self, tracker: OngoingCacheTracker):
        """The callback for finishing up caching.

        If caching is successful, and the space usage is reaching soft limit,
        we will try to ensure free space for already cached file.
        (No need to consider reaching hard limit, as the caching will be interrupted
        in half way and f.cached_success will be False.)

        If space cannot be ensured, the cached file will be delete.
        If caching fails, the unfinished cached file will be cleanup.

        Args:
            tracker: for cache writer, to sync status between subscriber.
        """
        if (meta := tracker.get_meta_on_success()) is None:
            return

        try:
            if not self._storage_below_soft_limit_event.is_set():
                # try to reserve space for the saved cache entry
                if self._reserve_space(meta.size):
                    if not self._lru_helper.commit_entry(meta):
                        logger.debug(f"failed to commit cache for {meta.url=}")
                else:
                    # case 2: cache successful, but reserving space failed,
                    # NOTE(20221018): let gc to remove the tmp file
                    # NOTE(20221205): still keep the tmp file for other subscribers
                    #                 until it has been gced(so set success=True)
                    logger.debug(f"failed to reserve space for {meta.url=}")
            else:
                # case 3: commit cache and finish up
                if not self._lru_helper.commit_entry(meta):
                    logger.debug(f"failed to commit cache entry for {meta.url=}")
        except Exception as e:
            logger.exception(f"failed on callback for {meta=}: {e!r}")

    def _process_raw_url(self, raw_url: str) -> str:
        # NOTE: raw_url(get from uvicorn) is unquoted, we must quote it again before we send it to the remote
        # NOTE(20221003): as otaproxy, we should treat all contents after netloc as path and not touch it,
        #                 because we should forward the request as it to the remote.
        # NOTE(20221003): unconditionally set scheme to https if enable_https, else unconditionally set to http
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

    async def _lookup_cachedb(
        self, raw_url: str, *, retry_cache: bool
    ) -> Optional[CacheMeta]:
        if meta_db_entry := await self._lru_helper.lookup_entry_by_url(
            raw_url, executor=self._executor
        ):  # cache hit
            logger.debug(f"cache hit for {raw_url=}\n, {meta_db_entry=}")
            cache_path: Path = self._base_dir / meta_db_entry.sha256hash
            # clear the cache entry if the ota_client instructs so
            if retry_cache:
                logger.warning(
                    f"retry_cache: try to clear entry for {meta_db_entry=}.."
                )
                cache_path.unlink(missing_ok=True)

            # check whether cache file is presented
            if not cache_path.is_file():
                # invalid cache entry found in the db, cleanup it
                logger.warning(f"dangling cache entry found: {meta_db_entry=}")
                await self._lru_helper.remove_entry_by_url(
                    raw_url, executor=self._executor
                )
                return
            return meta_db_entry

    ###### exposed API ######
    async def retrieve_file(
        self,
        raw_url: str,
        /,
        cookies: Dict[str, str],
        extra_headers: Dict[str, str],
        cache_control_policies: Set[OTAFileCacheControl],
    ) -> Optional[Tuple[AsyncIterator[bytes], CacheMeta]]:
        """Exposed API to retrieve a file descriptor.

        This method retrieves a file descriptor for incoming client request.
        Upper uvicorn app can use this file descriptor to yield chunks of data,
        and stream chunks to the on-calling ota_client.

        NOTE: use raw_url in all operations, except openning remote file.

        Args:
            raw_url: unquoted raw url received from uvicorn
            cookies: cookies in the incoming client request.
            extra_headers: headers in the incoming client request.
                Currently Cookies and Authorization headers are used.
            cache_control_policies: OTA-FILE-CACHE-CONTROL headers that
                controls how ota-proxy should handle the request.

        Returns:
            fd: a asyncio generator for upper server app to yield data chunks from
            meta: CacheMeta of requested file
        """
        if self._closed:
            raise BaseOTACacheError("ota cache pool is closed")

        # default cache control policy:
        retry_cache, use_cache = False, True
        # parse input policies
        if OTAFileCacheControl.retry_caching in cache_control_policies:
            retry_cache = True
            logger.warning(f"client indicates that cache for {raw_url=} is invalid")
        if OTAFileCacheControl.no_cache in cache_control_policies:
            logger.info(f"client indicates that do not cache for {raw_url=}")
            use_cache = False
        # if there is no upper_ota_proxy,
        # trim the custom headers away
        if self._enable_https:
            extra_headers.pop(OTAFileCacheControl.header.value, None)

        # case 1: not using cache, directly download file
        if (
            not self._cache_enabled  # ota_proxy is configured to not cache anything
            or not use_cache  # ota_client send request with no_cache policy
            or not self._storage_below_hard_limit_event.is_set()  # disable cache if space hardlimit is reached
        ):
            logger.debug(f"not use cache: {raw_url=}")
            # NOTE: store unquoted url in database
            meta = CacheMeta(url=raw_url)
            fd = _FileDescriptorHelper.open_remote(
                self._process_raw_url(raw_url),
                meta,  # updated when remote response is received
                cookies,
                extra_headers,
                session=self._session,
                upper_proxy=self._upper_proxy,
            )
            await fd.__anext__()  # open remote connection
            return fd, meta

        # cache enabled, lookup the database
        # case 2: cache is available and valid, use cache
        if meta_db_entry := await self._lookup_cachedb(
            raw_url, retry_cache=retry_cache
        ):
            logger.debug(f"use cache for {raw_url=}")
            fd = _FileDescriptorHelper.open_cached_file(
                self._base_dir / meta_db_entry.sha256hash, executor=self._executor
            )
            return fd, meta_db_entry

        # case 3: no valid cache entry presented, try to cache the requested file
        logger.debug(f"no cache entry for {raw_url=}")
        # case 3.1: download and cache new file
        _tracker, is_writer = await self._on_going_caching.get_or_subscribe_tracker(
            raw_url
        )
        if is_writer and _tracker is not None:  # writer/provider
            ota_file = RemoteOTAFile(
                self._process_raw_url(raw_url),
                raw_url,
                tracker=_tracker,
                below_hard_limit_event=self._storage_below_hard_limit_event,
                base_dir=self._base_dir,
            )
            return await ota_file.open_remote(
                self._register_cache_callback,
                cookies=cookies,
                extra_headers=extra_headers,
                session=self._session,
                upper_proxy=self._upper_proxy,
                executor=self._executor,
            )
        # case 3.2: respond by cache streaming
        elif _tracker is not None:  # reader/subscriber
            return (
                _FileDescriptorHelper.open_cache_stream(
                    _tracker,
                    base_dir=self._base_dir,
                    executor=self._executor,
                ),
                _tracker.meta,
            )  # type: ignore
        else:
            # NOTE: let the otaclient retry handle the edge condition
            return None
