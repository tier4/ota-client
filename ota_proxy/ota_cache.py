import asyncio
import aiofiles
import aiohttp
import shutil
import time
import weakref
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime
from functools import partial
from hashlib import sha256
from os import urandom
from pathlib import Path
from queue import Queue
from threading import Event, Lock
from typing import (
    Any,
    Callable,
    Dict,
    AsyncGenerator,
    List,
    Set,
    Tuple,
    Union,
)
from urllib.parse import quote, urlparse


from . import db
from .config import OTAFileCacheControl, config as cfg

import logging

logger = logging.getLogger(__name__)
logger.setLevel(cfg.LOG_LEVEL)


def get_backoff(n: int, factor: float, _max: float):
    return min(_max, factor * (2 ** (n - 1)))


class OngoingCacheTracker:
    def __init__(self, fn: str, ref_holder: Any):
        self.fn = fn
        self.writer_ready = asyncio.Event()
        self.cache_done = Event()
        self._is_failed = False
        self._ref_holer = ref_holder

    async def writer_on_ready(self, meta):
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
    def __init__(self, base_dir: str):
        self._base_dir = Path(base_dir)
        self._lock = Lock()
        self._url_ref_dict: Dict[bytes, asyncio.Event] = weakref.WeakValueDictionary()
        self._ref_tracker_dict: Dict[
            asyncio.Event, OngoingCacheTracker
        ] = weakref.WeakKeyDictionary()

    def _finalizer(self, fn: str):
        # cleanup(unlink) the tmp file
        (self._base_dir / fn).unlink(missing_ok=True)

    async def get_tracker(self, url: str) -> Tuple[OngoingCacheTracker, bool]:
        _ref = self._url_ref_dict.get(url)
        if _ref:
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
    """A helper class that provides API for accessing/managing cache entries in db.

    Serveral buckets are created according to predefined file size threshould.
    Each bucket will maintain the cache entries of that bucket's size definition,
    LRU is applied on per-bucket scale.

    NOTE: currently entry that has size larger than 512MiB or smaller that 1KiB will always be saved.
    """

    BSIZE_LIST = list(cfg.BUCKET_FILE_SIZE_DICT.keys())
    BSIZE_DICT = cfg.BUCKET_FILE_SIZE_DICT

    def __init__(self):
        self._db: db.OTACacheDB = db.DBProxy(cfg.DB_FILE)
        self._closed = False

    def _bin_search(self, file_size: int) -> int:
        """NOTE: The interval is Left-closed and right-opened."""
        if file_size < 0:
            raise ValueError(f"invalid file size {file_size}")

        s, e = 0, len(self.BSIZE_LIST) - 1
        if file_size < self.BSIZE_LIST[-1]:
            while True:
                if abs(e - s) <= 1:
                    break

                mid = (s + e) // 2
                if file_size < self.BSIZE_LIST[mid]:
                    e = mid
                else:
                    s = mid

            target_size = self.BSIZE_LIST[s]
        else:
            target_size = self.BSIZE_LIST[-1]

        return target_size

    def close(self):
        if not self._closed:
            self._db.close()

    def commit_entry(self, entry: db.CacheMeta) -> bool:
        """Commit cache entry meta to the database."""
        # populate bucket and last_access column
        entry.bucket = self._bin_search(entry.size)
        entry.last_access = datetime.now().timestamp()

        if self._db.insert_entry(entry) != 1:
            logger.warning(f"db: failed to add {entry=}")
            return False
        else:
            logger.debug(f"db: commit {entry=} successfully")
            return True

    def lookup_entry(self, url: str) -> db.CacheMeta:
        return self._db.lookup_url(url)

    def remove_entry(self, *, url: str = None, _hash: str = None) -> bool:
        """Remove one entry from database by url or hash."""
        if url and self._db.remove_entries_by_urls(url) == 1:
            return True

        if _hash and self._db.remove_entries_by_hashes(_hash) == 1:
            return True

        return False

    def rotate_cache(self, size: int) -> Union[List[str], None]:
        """Wrapper method for calling the database LRU cache rotating method.

        Args:
            size int: the size of file that we want to reserve space for

        Returns:
            A list of hashes that needed to be cleaned, or None if no enough entries.
        """
        # NOTE: currently file size >= 512MiB or file size < 1KiB
        # will be saved without cache rotating.
        if size >= self.BSIZE_LIST[-1] or size < self.BSIZE_LIST[1]:
            return []

        _cur_bucket_size = self._bin_search(size)
        _cur_bucket_idx = self.BSIZE_LIST.index(_cur_bucket_size)

        # first check the upper bucket
        for _bucket_size in self.BSIZE_LIST[_cur_bucket_idx + 1 :]:
            res = self._db.rotate_cache(_bucket_size, self.BSIZE_DICT[_bucket_size])
            if res:
                return res

        # if cannot find one entry at any upper bucket, check current bucket
        return self._db.rotate_cache(
            _cur_bucket_size, self.BSIZE_DICT[_cur_bucket_size]
        )


class OTAFile:
    """File descriptor for data streaming.

    Instance of OTAFile wraps meta data for specific URL,
    along with a file descriptor(a AsyncGenerator) that can be used
    to yield chunks of data from.
    Instance of OTAFile is requested by the upper uvicorn app,
    and being created and passed to app by OTACache instance.
    Check OTACache.retreive_file for details.

    Attributes:
        fp: an AsyncGenerator of opened resource, by opening
            local cached file or remote resource.
        meta db.CacheMeta: meta data of the resource indicated by URL.
        below_hard_limit_event Event: a Event instance that can be used to check whether
            local storage space is enough for caching.
    """

    PIPE_READ_BACKOFF_MAX: int = 6
    PIPE_READ_BACKOFF_FACTOR: int = 0.001

    def __init__(
        self,
        fp: AsyncGenerator[bytes, None],
        meta: db.CacheMeta,
        *,
        below_hard_limit_event: Event = None,
    ):
        self._base_dir = Path(cfg.BASE_DIR)
        self._storage_below_hard_limit = below_hard_limit_event
        # NOTE: the hash and size in the meta are not set yet
        self.meta = meta
        self._fp = fp

        # life cycle
        self.closed = Event()
        # whether the fp finishes its work(successful or not)
        self.cached_success = False
        self._cache_tee_aborted: Event = Event()
        self._queue: Queue = Queue()

    def background_write_cache(
        self,
        tracker: OngoingCacheTracker,
        callback: "Callable[[OTAFile,OngoingCacheTracker], None]",
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
            callback: a callback function to do post-caching jobs
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
                        try:
                            _timout = get_backoff(
                                err_count,
                                self.PIPE_READ_BACKOFF_FACTOR,
                                self.PIPE_READ_BACKOFF_MAX,
                            )
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
                self.meta.hash = _hash
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

    async def get_chunks(self) -> AsyncGenerator[bytes, None]:
        """API for caller to yield data chunks from.

        This method yields data chunks from selves' file descriptor,
        and then streams data chunks to upper caller and caching thread(if cache is enabled)
        similar to the linux command tee does.

        Returns:
            An AsyncGenerator for upper caller to yield data chunks from.
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
    """Helper class to scrub ota caches."""

    DB_FILE: str = cfg.DB_FILE
    BASE_DIR: str = cfg.BASE_DIR

    def __init__(self):
        self._db = db.OTACacheDB(self.DB_FILE)
        self._excutor = ProcessPoolExecutor()
        self._closed = False

    @staticmethod
    def _check_entry(base_dir: str, meta: db.CacheMeta) -> Union[db.CacheMeta, bool]:
        f = Path(base_dir) / meta.hash
        if f.is_file():
            hash_f = sha256()
            # calculate file's hash and check against meta
            with open(f, "rb") as fp:
                while True:
                    data = fp.read(cfg.CHUNK_SIZE)
                    if len(data) > 0:
                        hash_f.update(data)
                    else:
                        break

            if hash_f.hexdigest() == meta.hash:
                return meta, True

        # check failed, try to remove the cache entry
        f.unlink(missing_ok=True)
        return meta, False

    def _close(self):
        if not self._closed:
            self._excutor.shutdown(wait=True)
            self._db.close()
            self._closed = True

    def __call__(self):
        """Main entry for scrubbing cache folder."""
        if self._closed:
            return

        logger.info("start to scrub the cache entries...")
        try:
            dangling_db_entry = []
            # NOTE: pre-add db related files into the set
            # to prevent db related files being deleted
            # NOTE 2: cache_db related files: <cache_db>, <cache_db>-shm, <cache_db>-wal, <cache>-journal
            db_file = Path(self.DB_FILE).name
            valid_cache_entry = {
                db_file,
                f"{db_file}-shm",
                f"{db_file}-wal",
                f"{db_file}-journal",
            }
            res_list = self._excutor.map(
                partial(self._check_entry, self.BASE_DIR),
                self._db.lookup_all(),
                chunksize=128,
            )

            for meta, valid in res_list:
                if not valid:
                    logger.debug(f"invalid db entry found: {meta.url}")
                    dangling_db_entry.append(meta.url)
                else:
                    valid_cache_entry.add(meta.hash)

            # delete the invalid entry from the database
            self._db.remove_entries_by_urls(*dangling_db_entry)

            # loop over all files under cache folder,
            # if entry's hash is not presented in the valid_cache_entry set,
            # we treat it as dangling cache entry and delete it
            _base_dir_path = Path(self.BASE_DIR)
            for entry in _base_dir_path.glob("*"):
                if entry.name not in valid_cache_entry:
                    logger.warning(f"dangling cache entry found: {entry.name}")
                    f = _base_dir_path / entry.name
                    f.unlink(missing_ok=True)

            logger.info("cache scrub finished")
        except Exception as e:
            logger.error(f"failed to finish scrub cache folder: {e!r}")
        finally:
            self._close()


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
            default is False. NOTE: scheme change is applied unconditionally
        init_cache bool: whether to clear the existed cache, default is True
    """

    CACHE_STREAM_BACKOFF_FACTOR: float = 0.1
    CACHE_STREAM_TIMEOUT_MAX: float = 1

    def __init__(
        self,
        *,
        cache_enabled: bool,
        init_cache: bool,
        upper_proxy: str = None,
        enable_https: bool = False,
        scrub_cache_event: "Event" = None,
    ):
        """Init ota_cache instance with configurations."""
        logger.debug(
            f"init ota_cache({cache_enabled=}, {init_cache=}, {upper_proxy=}, {enable_https=})"
        )
        self._closed = True

        self._chunk_size = cfg.CHUNK_SIZE
        self._base_dir = Path(cfg.BASE_DIR)
        self._cache_enabled = cache_enabled
        self._init_cache = init_cache
        self._enable_https = enable_https
        self._executor = ThreadPoolExecutor(thread_name_prefix="ota_cache_executor")

        self._storage_below_hard_limit_event = Event()
        self._storage_below_soft_limit_event = Event()
        self._upper_proxy: str = upper_proxy

        self._scrub_cache_event = scrub_cache_event

    async def start(self):
        """Start the ota_cache instance."""
        # silently ignore multi launching of ota_cache
        if not self._closed:
            logger.warning("multiple launching of ota_cache detected, ignored")
            return

        # label as started
        self._closed = False

        # ensure base dir
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
                db.OTACacheDB(cfg.DB_FILE, init=True)
            else:
                # scrub the cache folder
                _scrub_cache = OTACacheScrubHelper()
                _scrub_cache()

            # dispatch a background task to pulling the disk usage info
            self._executor.submit(self._background_check_free_space)

            # init cache helper
            self._lru_helper = LRUCacheHelper()
            # init cache streaming provider
            self._on_going_caching = OngoingCachingRegister(cfg.BASE_DIR)

            if self._upper_proxy:
                # if upper proxy presented, force disable https
                self._enable_https = False

        else:
            self._cache_enabled = False
            # event cache is not set, still remember to set the scrub_cache_event!!!

        # set scrub_cache_event after init/scrub cache
        # TODO: passthrough the exception to the ota_client process
        # if the init/scrub failed
        if self._scrub_cache_event:
            self._scrub_cache_event.set()

        logger.debug(f"ota_cache started")

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
        """
        while not self._closed:
            try:
                disk_usage = shutil.disk_usage(self._base_dir)
                current_used_p = disk_usage.used / disk_usage.total
                if current_used_p < cfg.DISK_USE_LIMIT_SOFT_P:
                    logger.debug(f"storage usage below soft limit: {current_used_p}")
                    # below soft limit, normal caching mode
                    self._storage_below_soft_limit_event.set()
                    self._storage_below_hard_limit_event.set()
                elif (
                    current_used_p >= cfg.DISK_USE_LIMIT_SOFT_P
                    and current_used_p < cfg.DISK_USE_LIMIT_HARD_P
                ):
                    logger.info(f"storage usage below hard limit: {current_used_p}")
                    # reach soft limit but not reach hard limit
                    # space reservation will be triggered after new file cached
                    self._storage_below_soft_limit_event.clear()
                    self._storage_below_hard_limit_event.set()
                else:
                    logger.warning(f"storage usage reach hard limit: {current_used_p}")
                    # reach hard limit
                    # totally disable caching
                    self._storage_below_soft_limit_event.clear()
                    self._storage_below_hard_limit_event.clear()
            except FileNotFoundError:
                logger.error(
                    f"background free space check failed as cache folder disappeared"
                )
                self._storage_below_soft_limit_event.clear()
                self._storage_below_hard_limit_event.clear()

            time.sleep(cfg.DISK_USE_PULL_INTERVAL)

    def _cache_entries_cleanup(self, entry_hashes: List[str]) -> bool:
        """Cleanup entries indicated by entry_hashes list.

        Return:
            A bool indicates whether the cleanup is successful or not.
        """
        for entry_hash in entry_hashes:
            # remove cache entry
            f = self._base_dir / entry_hash
            f.unlink(missing_ok=True)
        return True

    def _reserve_space(self, size: int) -> bool:
        """A helper that calls lru_helper's rotate_cache method.

        Return:
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
            f: instance of OTAFile that registered with this callback
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
    async def _local_fp_stream(self, fpath, tracker: OngoingCacheTracker):
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

    async def _open_fp_by_cache_streaming(self, tracker: OngoingCacheTracker):
        # wait for writer to become ready
        try:
            await asyncio.wait_for(
                tracker.writer_ready.wait(), timeout=self.CACHE_STREAM_TIMEOUT_MAX
            )
        except asyncio.TimeoutError:
            raise TimeoutError(f"timeout waiting for {tracker.fn}") from None

        fpath = self._base_dir / tracker.fn
        return self._local_fp_stream(fpath, tracker)

    async def _local_fp(self, fpath):
        async with aiofiles.open(fpath, "rb", executor=self._executor) as f:
            while True:
                data = await f.read(self._chunk_size)
                if len(data) > 0:
                    yield data
                else:
                    break

    async def _open_fp_by_cache(self, _hash: str) -> AsyncGenerator[bytes, None]:
        """Opens file descriptor by opening local cached entry.

        Args:
            meta CacheMeta: meta for target file that queried from the db.

        Returns:
            An AsyncGenerator that can be yield data chunks from.

        Raises:
            FileNotFoundError: Raised if the file indicates by the meta doesn't exist.
        """
        fpath = self._base_dir / _hash
        if fpath.is_file():
            return self._local_fp(fpath)
        else:
            raise FileNotFoundError(f"cache entry {_hash} doesn't exist!")

    async def _remote_fp(self, url, raw_url, cookies, extra_headers):
        async with self._session.get(
            url, proxy=self._upper_proxy, cookies=cookies, headers=extra_headers
        ) as response:
            # assembling output cachemeta
            # NOTE: output cachemeta doesn't have hash, size, bucket, last_access defined set yet
            # NOTE.2: store the original unquoted url into the CacheMeta
            # NOTE.3: hash, and size will be assigned at background_write_cache method
            # NOTE.4: bucket and last_access will be assigned at commit_entry method
            yield db.CacheMeta(
                url=raw_url,
                content_encoding=response.headers.get("content-encoding", ""),
                content_type=response.headers.get(
                    "content-type", "application/octet-stream"
                ),
            )

            async for data, _ in response.content.iter_chunks():
                yield data

    async def _open_fp_by_requests(
        self,
        raw_url: str,
        cookies: Dict[str, str],
        extra_headers: Dict[str, str],
        tracker: OngoingCacheTracker = None,
    ) -> "Tuple[AsyncGenerator[bytes, None], db.CacheMeta]":
        """Opens file descriptor by opening connection to the remote OTA file server.

        Args:
            raw_url str: raw url that extracted from the client request, it will be parsed
                and adjusted(currently only scheme might be changed).
            cookies Dict[str, str]: cookies that extracted from the client request,
                we also need to pass these to the remote server.
            extra_headers Dict[str, str]: same as above.

        Returns:
            An AsyncGenerator and the generated meta from the response of remote server.

        Raises:
            Any errors that happen during open/handling the connection to the remote server.
        """
        url_parsed = urlparse(raw_url)

        # NOTE: raw_url is unquoted, we must quote it again before we send it to the remote
        url_parsed = url_parsed._replace(path=quote(url_parsed.path))
        if self._enable_https:
            url_parsed = url_parsed._replace(scheme="https")
        else:
            url_parsed = url_parsed._replace(scheme="http")

        url = url_parsed.geturl()

        # if there is no upper_ota_proxy,
        # trim the custom headers away
        if self._enable_https:
            extra_headers.pop(OTAFileCacheControl.header.value, None)

        # start the fp
        try:
            res = self._remote_fp(url, raw_url, cookies, extra_headers)
            meta = await res.__anext__()

            if tracker is not None:
                # prepare the temp file in advance(touch the file)
                (self._base_dir / tracker.fn).write_bytes(b"")
                # bind meta to tracker
                await tracker.writer_on_ready(meta)
        except Exception as e:
            # signal other subscribers to drop this tracker
            if tracker is not None:
                tracker.writer_on_failed()

            raise e from None

        # return the generator and the meta
        return res, meta

    ###### exposed API ######
    async def retrieve_file(
        self,
        url: str,
        /,
        cookies: Dict[str, str],
        extra_headers: Dict[str, str],
        cache_control_policies: Set[OTAFileCacheControl],
    ) -> "Tuple[AsyncGenerator[bytes, None], db.CacheMeta]":
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
        meta_db_entry = self._lru_helper.lookup_entry(url)
        if meta_db_entry:  # cache hit
            logger.debug(f"cache hit for {url=}\n, {meta_db_entry=}")

            cache_path: Path = self._base_dir / meta_db_entry.hash
            # clear the cache entry if the ota_client instructs so
            if retry_cache:
                logger.warning(
                    f"retry_cache: try to clear entry for {meta_db_entry=}.."
                )
                cache_path.unlink(missing_ok=True)

            if not cache_path.is_file():
                # invalid cache entry found in the db, cleanup it
                logger.warning(f"dangling cache entry found: {meta_db_entry=}")
                self._lru_helper.remove_entry(url=url)
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
        else:
            logger.debug(f"use cache for {url=}")
            fp = await self._open_fp_by_cache(meta_db_entry.hash)
            # use cache
            return fp, meta_db_entry
