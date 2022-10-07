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
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
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
    cast,
)
from urllib.parse import SplitResult, quote, urlsplit

from .cache_control import OTAFileCacheControl
from ._db import CacheMeta, OTACacheDB, OTACacheDBProxy
from .config import config as cfg

if TYPE_CHECKING:
    import multiprocessing

logger = logging.getLogger(__name__)
logger.setLevel(cfg.LOG_LEVEL)


def get_backoff(n: int, factor: float, _max: float) -> float:
    return min(_max, factor * (2 ** (n - 1)))


_WEAKREF = TypeVar("_WEAKREF")


class OngoingCacheTracker(Generic[_WEAKREF]):
    """A tracker for an on-going cache entry.

    This entry will disappear automatically when writer finished
    caching and commit the cache to the database.
    """

    def __init__(self, fn: str, ref_holder: _WEAKREF):
        self.fn = fn
        self.writer_ready = asyncio.Event()
        self.cache_done = threading.Event()
        self._is_failed = False
        self._ref_holer = ref_holder

    async def writer_on_ready(self, meta: CacheMeta):
        """Register meta to the Tracker and get ready."""
        self.meta = meta
        self.writer_ready.set()

    def writer_on_finish(self):
        """NOTE: must ensure cache entry is committed into the database
            before calling this function!
        NOTE 2: this method is cross-thread method
        """
        if not self.cache_done.is_set():
            self.cache_done.set()
            del self._ref_holer

    def writer_on_failed(self):
        """NOTE: this method is cross-thread method"""
        logger.warning(f"writer failed on {self.fn}, abort...")
        self._is_failed = True

        if not self.cache_done.is_set():
            self.cache_done.set()
            del self._ref_holer


class OngoingCachingRegister:
    """A tracker register class that provides cache streaming
        on same requested file from multiple caller.

    This tracker is implemented based on provider/subscriber model,
        with weakref to automatically cleanup finished sessions.
    """

    def __init__(self, base_dir: Union[str, Path]):
        self._base_dir = Path(base_dir)
        self._lock = threading.Lock()
        self._url_ref_dict = cast(
            Dict[str, asyncio.Event], weakref.WeakValueDictionary()
        )
        self._ref_tracker_dict = cast(
            Dict[asyncio.Event, OngoingCacheTracker], weakref.WeakKeyDictionary()
        )

    def _finalizer(self, fn: str):
        # cleanup(unlink) the tmp file when corresponding tracker is gced.
        (self._base_dir / fn).unlink(missing_ok=True)

    async def get_tracker(self, url: str) -> Tuple[OngoingCacheTracker, bool]:
        if _ref := self._url_ref_dict.get(url):
            await _ref.wait()  # wait for writer to fully initialized
            _tracker = self._ref_tracker_dict[_ref]
            return _tracker, False
        else:
            _tmp_fn = f"tmp_{urandom(16).hex()}"

            _ref = asyncio.Event()
            self._url_ref_dict[url] = _ref

            _tracker = OngoingCacheTracker(_tmp_fn, _ref)
            self._ref_tracker_dict[_ref] = _tracker
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

    def lookup_entry_by_url(self, url: str) -> Optional[CacheMeta]:
        return self._db.lookup_entry(CacheMeta.url, url)

    def remove_entry_by_url(self, url: str) -> bool:
        return self._db.remove_entries(CacheMeta.url, url) > 0

    def remove_entry_by_hash(self, _hash: str) -> bool:
        return self._db.remove_entries(CacheMeta.sha256hash, _hash) > 0

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


class OTAFile:
    """File descriptor for data streaming.

    Instance of OTAFile wraps meta data for specific URL,
    along with a file descriptor(an AsyncIterator) that can be used
    to yield chunks of data from.
    Instance of OTAFile is requested by the upper uvicorn app,
    and being created and passed to app by OTACache instance.
    Check OTACache.retreive_file for details.

    Attributes:
        fp: an AsyncIterator of opened resource, by opening
            local cached file or remote resource.
        meta CacheMeta: meta data of the resource indicated by URL.
        below_hard_limit_event Event: a Event instance that can be used to check whether
            local storage space is enough for caching.
    """

    PIPE_READ_BACKOFF_MAX = 6
    PIPE_READ_BACKOFF_FACTOR = 0.001

    def __init__(
        self,
        fp: AsyncIterator[bytes],
        meta: CacheMeta,
        *,
        below_hard_limit_event: threading.Event,
        base_dir: Union[str, Path],
    ):
        self._base_dir = Path(base_dir)
        self._storage_below_hard_limit = below_hard_limit_event
        # NOTE: the hash and size in the meta are not set yet
        self.meta = meta
        self._fp = fp

        # life cycle
        self.closed = threading.Event()
        # whether the fp finishes its work(successful or not)
        self.cached_success = False
        self._cache_tee_aborted = threading.Event()
        self._queue = Queue()

    def background_write_cache(
        self,
        tracker: OngoingCacheTracker,
        callback: "Callable[[OTAFile, OngoingCacheTracker], None]",
    ):
        """Caching files on to the local disk in the background thread.

        When OTAFile instance initialized and launched,
        a queue is opened between this method and the wrapped fp generator.
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
            self.temp_fpath = self._base_dir / tracker.fn

            with open(self.temp_fpath, "wb") as dst_f:
                err_count = 0
                while not self.closed.is_set() or not self._queue.empty():
                    if not self._storage_below_hard_limit.is_set():
                        # reach storage hard limit, abort caching
                        logger.debug(
                            f"not enough free space during caching url={self.meta.url}, abort"
                        )

                        # signal the streaming coro
                        # to stop streaming to the caching thread
                        self._cache_tee_aborted.set()
                    else:
                        _timout = get_backoff(
                            err_count,
                            self.PIPE_READ_BACKOFF_FACTOR,
                            self.PIPE_READ_BACKOFF_MAX,
                        )
                        try:
                            data = self._queue.get(timeout=_timout)
                            err_count = 0

                            if len(data) > 0:
                                _sha256hash_f.update(data)
                                _size += dst_f.write(data)
                        except Exception:
                            if _timout >= self.PIPE_READ_BACKOFF_MAX:
                                # abort caching due to potential dead streaming coro
                                logger.error(
                                    f"failed to cache {self.meta.url}: timeout getting data from queue"
                                )
                                self._cache_tee_aborted.set()

                                break
                            else:
                                err_count += 1

            # post caching
            if self.closed.is_set() and not self._cache_tee_aborted.is_set():
                # label this OTAFile as successful
                self.cached_success = True

                _hash = _sha256hash_f.hexdigest()
                self.meta.sha256hash = _hash
                self.meta.size = _size

                # for 0 size file, register the entry only
                # but if the 0 size file doesn't exist, create one
                if _size > 0 or not (self._base_dir / _hash).is_file():
                    logger.debug(f"successfully cached {self.meta.url}")
                    # NOTE: hardlink to new file name,
                    # tmp file cleanup will be conducted by CachingTracker
                    try:
                        self.temp_fpath.link_to(self._base_dir / _hash)
                    except FileExistsError:
                        # this might caused by entry with same file contents
                        logger.debug(f"entry {_hash} existed, ignored")

            # NOTE: if queue is empty but self._finished is not set,
            # it may indicate that an unfinished caching might happen

        except Exception as e:
            logger.debug(f"failed on writing cache for {tracker.fn}: {e!r}")
        finally:
            # NOTE: always remember to call callback
            callback(self, tracker)

    async def get_chunks(self) -> AsyncIterator[bytes]:
        """API for caller(server App) to yield data chunks from.

        This method yields data chunks from selves' file descriptor,
        and then streams data chunks to upper caller and caching thread(if cache is enabled)
        similar to the linux command tee does.

        Returns:
            An AsyncIterator for upper caller to yield data chunks from.
        """
        if self.closed.is_set():
            raise ValueError("file is closed")

        try:
            async for chunk in self._fp:
                # to caching thread
                # stop teeing data chunk to caching thread
                # if caching thread indicates so
                if not self._cache_tee_aborted.is_set():
                    self._queue.put_nowait(chunk)

                # to uvicorn thread
                yield chunk
        except Exception as e:
            # if any exception happens, signal the caching thread
            self._cache_tee_aborted.set()

            logger.exception("cache tee failed")
            raise e from None
        finally:
            # always close the file if get_chunk finished
            self.closed.set()


class OTACacheScrubHelper:
    """Helper to scrub ota caches."""

    def __init__(self, db_file: Union[str, Path], base_dir: Union[str, Path]):
        self._db = OTACacheDB(db_file)
        self._db_file = Path(db_file)
        self._base_dir = Path(base_dir)
        self._excutor = ProcessPoolExecutor()

    @staticmethod
    def _check_entry(base_dir: Path, meta: CacheMeta) -> Tuple[CacheMeta, bool]:
        f = base_dir / meta.sha256hash
        if f.is_file():
            hash_f = sha256()
            # calculate file's hash and check against meta
            with open(f, "rb") as fp:
                while data := fp.read(cfg.CHUNK_SIZE):
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
                    logger.warning(f"dangling cache entry found: {entry.name}")
                    f = self._base_dir / entry.name
                    f.unlink(missing_ok=True)

            logger.info("cache scrub finished")
        except Exception as e:
            logger.error(f"failed to finish scrub cache folder: {e!r}")
        finally:
            self._db.close()
            self._excutor.shutdown(wait=True)


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

    CACHE_STREAM_BACKOFF_FACTOR: float = 0.1
    CACHE_STREAM_TIMEOUT_MAX: float = 1

    def __init__(
        self,
        *,
        cache_enabled: bool,
        init_cache: bool,
        base_dir: Optional[Union[str, Path]] = None,
        db_file: Optional[Union[str, Path]] = None,
        upper_proxy: Optional[str] = None,
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
        timeout = aiohttp.ClientTimeout(total=None, sock_read=1)
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

        else:
            self._cache_enabled = False

        # set scrub_cache_event after init/scrub cache to signal the ota-client
        # TODO: passthrough the exception to the ota_client process
        #   if the init/scrub failed
        if self._scrub_cache_event:
            self._scrub_cache_event.set()

        logger.debug("ota_cache started")

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
                    logger.info(
                        f"storage usage below hard limit: {current_used_p:.2f}%"
                    )
                    # reach soft limit but not reach hard limit
                    # space reservation will be triggered after new file cached
                    self._storage_below_soft_limit_event.clear()
                    self._storage_below_hard_limit_event.set()
                else:
                    logger.warning(
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

    def _register_cache_callback(self, f: OTAFile, tracker: OngoingCacheTracker):
        """The callback for finishing up caching.

        All caching should end up here, whether caching is successful or not.

        If caching is successful, and the space usage is reaching soft limit,
        we will try to ensure free space for already cached file.
        (No need to consider reaching hard limit, as the caching will be interrupted
        in half way and f.cached_success will be False.)

        If space cannot be ensured, the cached file will be delete.

        If caching fails, the unfinished cached file will be cleanup.

        Args:
            f: instance of OTAFile that registered with this callback.
            tracker: for cache writer, to sync status between subscriber.
        """
        meta = f.meta
        try:
            if f.cached_success:
                logger.debug(
                    f"caching successfully for {meta.url=}, try to commit cache..."
                )
                if not self._storage_below_soft_limit_event.is_set():
                    # try to reserve space for the saved cache entry
                    if self._reserve_space(meta.size):
                        # case 1: commit cache and finish up
                        if self._lru_helper.commit_entry(meta):
                            tracker.writer_on_finish()
                        else:
                            tracker.writer_on_failed()
                    else:
                        # case 2: cache successful, but reserving space failed,
                        # cleanup cache file
                        logger.debug(
                            f"failed to reserve space for {meta.url=}, cleanup"
                        )
                        Path(f.temp_fpath).unlink(missing_ok=True)
                        tracker.writer_on_finish()
                else:
                    # case 3: commit cache and finish up
                    if self._lru_helper.commit_entry(meta):
                        tracker.writer_on_finish()
                    else:
                        tracker.writer_on_failed()
            else:
                # case 4: cache failed, cleanup dangling cache file
                logger.debug(f"cache for {meta.url=} failed, cleanup")
                Path(f.temp_fpath).unlink(missing_ok=True)

                tracker.writer_on_failed()

        except Exception as e:
            tracker.writer_on_failed()
            logger.exception(f"failed on callback for {meta=}: {e!r}")

    ###### create fp ######
    async def _local_fp_stream(
        self, fpath: Path, tracker: OngoingCacheTracker
    ) -> AsyncIterator[bytes]:
        """Open a file descriptor from an on-going cache file.

        Args:
            fpath: path to the cache entry.
            tracker: for subscriber to sync status with the provider(cache writer).

        Returns:
            An AsyncIterator that can yield data chunks from.

        Raises:
            FileNotFoundError if cache entry doesn't exist,
                or TimeoutError if subscriber timeout waiting for new data chunks
                to arrive.
        """
        async with aiofiles.open(fpath, "rb", executor=self._executor) as f:
            retry_count, done = 0, False
            while True:
                data = await f.read(self._chunk_size)
                if len(data) > 0:
                    retry_count = 0
                    yield data
                else:
                    # if writer indicates caching finished
                    if tracker.cache_done.is_set():
                        if tracker._is_failed:
                            raise ValueError(f"corrupted cache on {tracker.fn}")

                        # sleep and retry read data to ensure
                        # no data chunk is missed
                        if not done:
                            await asyncio.sleep(0.01)
                            done = True
                            continue

                        # stream finish, break out
                        break

                    # writer blocked, retry
                    else:
                        _wait = get_backoff(
                            retry_count,
                            self.CACHE_STREAM_BACKOFF_FACTOR,
                            self.CACHE_STREAM_TIMEOUT_MAX,
                        )

                        if _wait < self.CACHE_STREAM_TIMEOUT_MAX:
                            await asyncio.sleep(_wait)
                            retry_count += 1
                        else:
                            # timeout on streaming cache entry
                            raise TimeoutError(f"timeout streaming on {tracker.fn}")

    async def _local_fp(self, fpath):
        async with aiofiles.open(fpath, "rb", executor=self._executor) as f:
            while data := await f.read(self._chunk_size):
                yield data

    async def _open_fp_by_cache(self, _hash: str) -> AsyncIterator[bytes]:
        """Opens file descriptor by opening local cached entry.

        Args:
            _hash: target cache entry's hash value.

        Returns:
            An AsyncIterator that can yield data chunks from.

        Raises:
            FileNotFoundError: Raised if the file indicates by the meta doesn't exist.
        """
        fpath = self._base_dir / _hash
        if fpath.is_file():
            return self._local_fp(fpath)
        raise FileNotFoundError(f"cache entry {_hash} doesn't exist!")

    async def _remote_fp(
        self,
        url: str,
        raw_url: str,
        cookies: Dict[str, str],
        extra_headers: Dict[str, str],
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
        async with self._session.get(
            url, proxy=self._upper_proxy, cookies=cookies, headers=extra_headers
        ) as response:
            # assembling output cachemeta
            # NOTE: output cachemeta doesn't have hash, size, bucket, last_access defined set yet
            # NOTE.2: store the original unquoted url into the CacheMeta
            # NOTE.3: hash, and size will be assigned at background_write_cache method
            # NOTE.4: bucket and last_access will be assigned at commit_entry method
            yield CacheMeta(
                url=raw_url,
                content_encoding=response.headers.get("content-encoding", ""),
                content_type=response.headers.get(
                    "content-type", "application/octet-stream"
                ),
            )  # type: ignore

            async for data, _ in response.content.iter_chunks():
                yield data

    async def _open_fp_by_cache_streaming(
        self, tracker: OngoingCacheTracker
    ) -> AsyncIterator[bytes]:
        """Open file descriptor on an on-going cache file.

        Args:
            tracker: an instance of OngoingCacheTracker for subscriber
                to get information about the requested on-going cache entry.

        Returns:
            An AsyncIterator that can yield data chunks from.
        """
        # wait for writer to become ready
        await asyncio.wait_for(
            tracker.writer_ready.wait(), timeout=self.CACHE_STREAM_TIMEOUT_MAX
        )

        fpath = self._base_dir / tracker.fn
        return self._local_fp_stream(fpath, tracker)

    async def _open_fp_by_requests(
        self,
        raw_url: str,
        cookies: Dict[str, str],
        extra_headers: Dict[str, str],
        tracker: Optional[OngoingCacheTracker] = None,
    ) -> "Tuple[AsyncIterator[bytes], CacheMeta]":
        """Open file descriptor by opening connection to the remote OTA file server.

        Args:
            raw_url str: raw url that extracted from the client request, it will be parsed
                and adjusted(currently only scheme might be changed).
            cookies Dict[str, str]: cookies that extracted from the client request,
                we also need to pass these to the remote server.
            extra_headers Dict[str, str]: same as above.
            tracker: an OngoingCacheTracker that supports cache streaming on same requested file.

        Returns:
            An AsyncIterator and the generated meta from the response of remote server.

        Raises:
            Any errors that happen during open/handling the connection to the remote server.
        """
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
        url = SplitResult(
            scheme="https" if self._enable_https else "http",
            netloc=_raw_parse.netloc,
            path=quote(raw_url.replace(_raw_base, "", 1)),
            query="",
            fragment="",
        ).geturl()

        # if there is no upper_ota_proxy,
        # trim the custom headers away
        if self._enable_https:
            extra_headers.pop(OTAFileCacheControl.header.value, None)

        # start the fp
        try:
            res = self._remote_fp(url, raw_url, cookies, extra_headers)
            meta: CacheMeta = await res.__anext__()  # type: ignore

            if tracker is not None:
                # prepare the temp file in advance(touch the file)
                (self._base_dir / tracker.fn).write_bytes(b"")
                # bind meta to tracker
                await tracker.writer_on_ready(meta)

            return res, meta
        except Exception as e:
            # signal other subscribers to drop this tracker
            if tracker is not None:
                tracker.writer_on_failed()

            raise e from None

    ###### exposed API ######
    async def retrieve_file(
        self,
        url: str,
        /,
        cookies: Dict[str, str],
        extra_headers: Dict[str, str],
        cache_control_policies: Set[OTAFileCacheControl],
    ) -> Optional[Tuple[AsyncIterator[bytes], CacheMeta]]:
        """Exposed API to retrieve a file descriptor.

        This method retrieves a file descriptor for incoming client request.
        Upper uvicorn app can use this file descriptor to yield chunks of data,
        and stream chunks to the on-calling ota_client.

        Args:
            url str
            cookies: cookies in the incoming client request.
            extra_headers: headers in the incoming client request.
                Currently Cookies and Authorization headers are used.
            cache_control_policies: OTA-FILE-CACHE-CONTROL headers that
                controls how ota-proxy should handle the request.

        Returns:
            fp: a asyncio generator for upper server app to yield data chunks from
            meta: CacheMeta of requested file
        """
        if self._closed:
            raise ValueError("ota cache pool is closed")

        # default cache control policy:
        retry_cache, use_cache = False, True
        # parse input policies
        if OTAFileCacheControl.retry_caching in cache_control_policies:
            retry_cache = True
            logger.warning(f"client indicates that cache for {url=} is invalid")
        if OTAFileCacheControl.no_cache in cache_control_policies:
            logger.info(f"client indicates that do not cache for {url=}")
            use_cache = False

        # case 1: not using cache, directly download file
        if (
            not self._cache_enabled  # ota_proxy is configured to not cache anything
            or not use_cache  # ota_client send request with no_cache policy
            or not self._storage_below_hard_limit_event.is_set()  # disable cache if space hardlimit is reached
        ):
            fp, meta = await self._open_fp_by_requests(url, cookies, extra_headers)
            return fp, meta

        # cache enabled, lookup the database
        no_cache_available = True
        if meta_db_entry := self._lru_helper.lookup_entry_by_url(url):  # cache hit
            logger.debug(f"cache hit for {url=}\n, {meta_db_entry=}")

            cache_path: Path = self._base_dir / meta_db_entry.sha256hash
            # clear the cache entry if the ota_client instructs so
            if retry_cache:
                logger.warning(
                    f"retry_cache: try to clear entry for {meta_db_entry=}.."
                )
                cache_path.unlink(missing_ok=True)

            if not cache_path.is_file():
                # invalid cache entry found in the db, cleanup it
                logger.warning(f"dangling cache entry found: {meta_db_entry=}")
                self._lru_helper.remove_entry_by_url(url)
            else:
                no_cache_available = False

        # case 2: no valid cache entry presented, try to cache the requested file
        if no_cache_available:
            logger.debug(f"no cache entry for {url=}")

            # case 2.1: download and cache new file
            _tracker, is_writer = await self._on_going_caching.get_tracker(url)
            if is_writer:
                _remote_fp, meta = await self._open_fp_by_requests(
                    url, cookies, extra_headers, _tracker
                )

                ota_file = OTAFile(
                    _remote_fp,
                    meta,
                    below_hard_limit_event=self._storage_below_hard_limit_event,
                    base_dir=self._base_dir,
                )

                # NOTE: bind the tracker to the callback function
                self._executor.submit(
                    ota_file.background_write_cache,
                    _tracker,
                    self._register_cache_callback,
                )

                fp = ota_file.get_chunks()
                return fp, meta

            # case 2.2: respond by cache streaming
            else:
                # if this tracker is failed, drop it
                if _tracker._is_failed:
                    raise ValueError(f"cache corrupted on {_tracker.fn}, abort")

                logger.debug(f"enable cache streaming for {url=}")
                fp = await self._open_fp_by_cache_streaming(_tracker)
                return fp, _tracker.meta

        # case 3: cache is available and valid, use cache
        elif meta_db_entry:
            logger.debug(f"use cache for {url=}")
            fp = await self._open_fp_by_cache(meta_db_entry.sha256hash)
            # use cache
            return fp, meta_db_entry
