import aiofiles
import aiohttp
import subprocess
import shlex
import shutil
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime
from functools import partial
from hashlib import sha256
from os import urandom
from pathlib import Path
from queue import Queue
from threading import Event
from typing import Callable, Dict, AsyncGenerator, List, Set, Tuple, Union

from . import db
from .config import OTAFileCacheControl, config as cfg


import logging

logger = logging.getLogger(__name__)
logger.setLevel(cfg.LOG_LEVEL)


def _subprocess_check_output(cmd: str, *, raise_exception=False) -> str:
    try:
        return (
            subprocess.check_output(shlex.split(cmd), stderr=subprocess.DEVNULL)
            .decode()
            .strip()
        )
    except subprocess.CalledProcessError:
        if raise_exception:
            raise
        return ""


class _Register(set):
    """A simple register to maintain on-going a set of on-going cache entries.

    NOTE: With GIL, set instance will not be corrupted by multi-threading access/update,
    and the only edge condition by dirty read is the multi caching to the same URL.
    Considering the performance lost and what edge condition may happen, no lock is used here.
    """

    def register(self, url: str) -> bool:
        if url in self:
            return False
        else:
            self.add(url)
            return True

    def unregister(self, url: str):
        self.discard(url)


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

    def commit_entry(self, entry: db.CacheMeta):
        """Commit cache entry meta to the database."""
        # populate bucket and last_access column
        entry.bucket = self._bin_search(entry.size)
        entry.last_access = datetime.now().timestamp()

        if self._db.insert_entry(entry) != 1:
            logger.warning(f"db: failed to add {entry=}")
        else:
            logger.debug(f"db: commit {entry=} successfully")

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
            A list of hashes that needed to be cleaned.
        """
        # NOTE: currently file size >= 512MiB or file size < 1KiB
        # will be saved without cache rotating.
        if size >= self.BSIZE_LIST[-1] or size < self.BSIZE_LIST[1]:
            return True

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
        url str: requested resource's URL.
        meta db.CacheMeta: meta data of the resource indicated by URL.
        fp AsyncGenerator: an AsyncGenerator of opened resource, by opening
            local cached file or remote resource.
        store_cache bool: whether to cache with opened file descriptor, default is False.
        below_hard_limit_event Event: a Event instance that can be used to check whether
            local storage space is enough for caching.
    """

    BACKOFF_MAX: int = 6
    BACKOFF_FACTOR: int = 0.001

    def __init__(
        self,
        url: str,
        meta: db.CacheMeta,
        fp: AsyncGenerator,
        *,
        store_cache=False,
        below_hard_limit_event: Event = None,
    ):
        logger.debug(f"new OTAFile request: {url}")

        self.backoff_max = self.BACKOFF_MAX
        self.backoff_factor = self.BACKOFF_FACTOR
        self._base_dir = Path(cfg.BASE_DIR)
        self._store_cache = store_cache
        self._storage_below_hard_limit = below_hard_limit_event
        # NOTE: for new cache entry meta, the hash and size are not set yet
        self.meta = meta
        self._fp = fp

        # life cycle
        self.closed: Event = (
            Event()
        )  # whether the fp finishes its work(successful or not)
        self.cached_success = False
        self._cache_aborted: Event = Event()

        # prepare for data streaming
        if store_cache:
            self._hash_f = sha256()
            self._queue: Queue = Queue()

    def _get_backoff(self, n):
        if n < 0:
            raise ValueError
        return min(self.backoff_max, self.backoff_factor * (2 ** (n - 1)))

    def background_write_cache(self, callback: "Callable[[OTAFile,], None]"):
        """Caching files on to the local disk in the background thread.

        When OTAFile instance initialized and launched,
        a queue is opened between this method and the wrapped fp generator.
        This method will receive data chunks from the queue, calculate the hash,
        and cache the data chunks onto the disk.
        Backoff retry is applied here to allow delays of data chunks arrived in the queue.

        This method should be called when the OTAFile instance is created.
        A callback method should be assigned when this method is called.

        Args:
            callback Callable[[OTAFile,], None]:
                a callback function to do post-caching jobs
        """
        if not self._store_cache or not self._storage_below_hard_limit:
            # call callback function even we don't cache anything
            # as we need to cleanup the status
            callback(self)

        try:
            logger.debug(f"start to cache for {self.meta.url}...")
            self.temp_fpath = self._base_dir / f"tmp_{urandom(16).hex()}"

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
                        self._cache_aborted.set()
                    else:
                        try:
                            _timout = self._get_backoff(err_count)
                            data = self._queue.get(timeout=_timout)

                            err_count = 0
                            if len(data) > 0:
                                self._hash_f.update(data)
                                self.meta.size += dst_f.write(data)
                        except Exception:
                            if _timout >= self.BACKOFF_MAX:
                                # abort caching due to potential dead streaming coro
                                logger.error(
                                    f"failed to cache {self.meta.url}: timeout getting data from queue"
                                )
                                self._cache_aborted.set()

                                break
                            else:
                                err_count += 1

            # post caching
            if self.closed.is_set() and not self._cache_aborted.is_set():
                # rename the file to the hash value
                hash = self._hash_f.hexdigest()
                self.meta.hash = hash
                self.cached_success = True

                # for 0 size file, register the entry only
                # but if the 0 size file doesn't exist, create one
                if self.meta.size > 0 or not (self._base_dir / hash).is_file():
                    logger.debug(f"successfully cached {self.meta.url}")
                    self.temp_fpath.rename(self._base_dir / hash)
                else:
                    self.temp_fpath.unlink(missing_ok=True)
            # NOTE: if queue is empty but self._finished is not set,
            # it may indicate that an unfinished caching might happen

        finally:
            # NOTE: always remember to call callback
            callback(self)

    async def get_chunks(self) -> AsyncGenerator:
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
                if self._store_cache:
                    if not self._cache_aborted.is_set():
                        self._queue.put_nowait(chunk)

                # to uvicorn thread
                yield chunk
        except Exception:
            # if any exception happens, signal the caching coro
            if self._store_cache:
                self._cache_aborted.set()

            raise
        finally:
            # always close the file if get_chunk finished
            self.closed.set()


class OTACacheHelper:
    """Helper class to scrub ota caches."""

    def __init__(self):
        self._base_dir = Path(cfg.BASE_DIR)
        self._db = db.OTACacheDB(cfg.DB_FILE)
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

    def scrub_cache(self):
        if self._closed:
            return

        logger.debug("start to scrub the cache entries...")

        dangling_db_entry = []
        # NOTE: pre-add database file into the set
        # to prevent db file being deleted
        valid_cache_entry = {Path(cfg.DB_FILE).name}
        res_list = self._excutor.map(
            partial(self._check_entry, str(self._base_dir)),
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
        self._db.remove_urls(*dangling_db_entry)

        # loop over all files under cache folder,
        # if entry's hash is not presented in the valid_cache_entry set,
        # we treat it as dangling cache entry and delete it
        for entry in self._base_dir.glob("*"):
            if entry.name not in valid_cache_entry:
                logger.debug(f"dangling cache entry found: {entry.name}")
                f = self._base_dir / entry.name
                f.unlink(missing_ok=True)

        # cleanup
        self._excutor.shutdown(wait=True)
        self._closed = True
        logger.debug("cache scrub finished")


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
        scrub_cache_event multiprocessing.Event: an Event instance for signaling the upper caller
            that the cache scrub finished and ota_cache is ready to handle requests.
        upper_proxy str: the upper proxy that ota_cache uses to send out request, default is None
        cache_enabled bool: when set to False, ota_cache will only relay requested data, default is False.
        enable_https bool: whether the ota_cache should send out the requests with HTTPS,
            default is False. NOTE: scheme change is applied unconditionally
        init_cache bool: whether to clear the existed cache, default is True
    """

    def __init__(
        self,
        *,
        scrub_cache_event,
        cache_enabled: bool,
        init_cache: bool,
        upper_proxy: str = None,
        enable_https: bool = False,
    ):
        logger.debug(
            f"init ota_cache({cache_enabled=}, {init_cache=}, {upper_proxy=}, {enable_https=})"
        )
        # event to signal upper caller that scrub finished
        self._scrub_cache_event = scrub_cache_event

        self._chunk_size = cfg.CHUNK_SIZE
        self._base_dir = Path(cfg.BASE_DIR)
        self._closed = False
        self._cache_enabled = cache_enabled
        self._enable_https = enable_https
        self._executor = ThreadPoolExecutor()

        self._storage_below_hard_limit_event = Event()
        self._storage_below_soft_limit_event = Event()
        self._on_going_caching = _Register()
        self._upper_proxy: str = ""

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

        if cache_enabled:
            self._cache_enabled = True
            self._bsize_list = cfg.BUCKET_FILE_SIZE_LIST
            self._buckets = Buckets()

            # prepare cache dire
            if init_cache:
                shutil.rmtree(str(self._base_dir), ignore_errors=True)
                # if init, we also have to set the scrub_finished_event
                self._base_dir.mkdir(exist_ok=True, parents=True)
            else:
                # scrub the cache folder in the background
                # NOTE: the _cache_helper is only used once here
                _cache_helper = OTACacheHelper()
                _cache_helper.scrub_cache()

            # signal the finish of cache scrubbing
            self._scrub_cache_event.set()

            # dispatch a background task to pulling the disk usage info
            self._executor.submit(self._background_check_free_space)

            # NOTE: type hint proxy class as its target class
            self._db: db.OTACacheDB = db.DBProxy(cfg.DB_FILE)

            if upper_proxy:
                # if upper proxy presented, we must disable https
                self._upper_proxy = upper_proxy
                self._enable_https = False

        else:
            # even cache is not enabled,
            # we still need to signal upper caller that cache scrub finished
            self._scrub_cache_event.set()
            self._cache_enabled = False

    def close(self):
        """Shutdowns OTACache instance.

        NOTE: cache folder cleanup on successful ota update is not
        performed by the OTACache.
        """
        logger.debug("shutdown ota-cache...")
        if self._cache_enabled and not self._closed:
            self._closed = True
            self._executor.shutdown(wait=True)
            self._db.close()

        logger.info("shutdown ota-cache completed")

    def _background_check_free_space(self):
        """Constantly checks the usage of disk where cache folder residented.

        This method calls "df --output=pcent <cache_folder>" to query the disk usage.
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
                cmd = f"df --output=pcent {self._base_dir}"
                current_used_p = _subprocess_check_output(cmd, raise_exception=True)

                # expected output:
                # 0: Use%
                # 1: 33%
                current_used_p = int(current_used_p.splitlines()[-1].strip(" %"))
                if current_used_p < cfg.DISK_USE_LIMIT_SOTF_P:
                    logger.debug(f"storage usage below soft limit: {current_used_p}")
                    # below soft limit, normal caching mode
                    self._storage_below_soft_limit_event.set()
                    self._storage_below_hard_limit_event.set()
                elif (
                    current_used_p >= cfg.DISK_USE_LIMIT_SOTF_P
                    and current_used_p < cfg.DISK_USE_LIMIT_HARD_P
                ):
                    logger.debug(f"storage usage below hard limit: {current_used_p}")
                    # reach soft limit but not reach hard limit
                    # space reservation will be triggered after new file cached
                    self._storage_below_soft_limit_event.clear()
                    self._storage_below_hard_limit_event.set()
                else:
                    logger.debug(f"storage usage reach hard limit: {current_used_p}")
                    # reach hard limit
                    # totally disable caching
                    self._storage_below_soft_limit_event.clear()
                    self._storage_below_hard_limit_event.clear()
            except Exception as e:
                logger.warning(f"background free space check failed: {e!r}")
                self._storage_below_soft_limit_event.clear()
                self._storage_below_hard_limit_event.clear()

            time.sleep(cfg.DISK_USE_PULL_INTERVAL)

    def _commit_cache(self, m: db.CacheMeta):
        """Commits the cache entry(CacheMeta) to the db."""
        logger.debug(f"commit cache for {m.url}...")
        bucket = self._buckets.get_bucket(m.size)
        bucket.add_entry(m.hash)

        # register to the database
        self._db.insert_urls(m)

    def _register_cache_callback(self, f: OTAFile):
        """The callback for finishing up caching.

        All caching should end up here, whether caching is successful or not.
        If caching is successful, and the space usage is reaching soft limit,
        we will try to ensure free space for already cached file.
        (No need to consider reaching hard limit, as the caching will be interrupted
        in half way and f.cached_success will be False.)
        If space cannot be ensured, the cached file will be delete.
        If caching fails, the unfinished cached file will be cleanup.

        Args:
            fut asyncio.Future: the Future object of excution of caching,
                we can retrieve the target instance of OTAFile from it.
        """
        meta = f.meta
        if f.cached_success:
            logger.debug(
                f"caching successfully for {meta.url=}, try to commit cache..."
            )
            if not self._storage_below_soft_limit_event.is_set():
                if self._ensure_free_space(meta.size):
                    self._commit_cache(meta)
                else:
                    # failed to reserve space,
                    # cleanup cache file
                    logger.debug(f"failed to reserve space for {meta.url=}, cleanup")
                    Path(f.temp_fpath).unlink(missing_ok=True)
            else:
                self._commit_cache(meta)
        else:
            # cache failed,
            # cleanup dangling cache file
            if meta.size == 0:
                logger.debug(f"skip caching 0 size file {meta.url=}")
            else:
                logger.debug(f"cache for {meta.url=} failed, cleanup")
            Path(f.temp_fpath).unlink(missing_ok=True)

        # always remember to remove url in the on_going_cache_list!
        self._on_going_caching.unregister(meta.url)

    def _ensure_free_space(self, size: int) -> bool:
        """Reserves free space of <size> for newly cached file in LRU flavour.

        Args:
            size int: size of the target file.

        Returns:
            A bool indicates the reservation is successful or not.
        """
        target_size = self._buckets._bin_search(size)
        bucket = self._buckets[target_size]

        # first check the current bucket
        hash_list = bucket.reserve_space(size)
        if hash_list:
            self._db.remove_url_by_hash(*hash_list)
            return True

        else:  # if current bucket is not enough, check higher bucket
            entry_to_clear = None
            for bs in self._bsize_list[self._bsize_list.index(target_size) + 1 :]:
                bucket = self._buckets[bs]
                entry_to_clear = bucket.popleft()

            if entry_to_clear:
                # get one entry from the target bucket
                # and then delete it
                f = self._base_dir / entry_to_clear
                f.unlink(missing_ok=True)
                self._db.remove_url_by_hash(entry_to_clear)

                return True

        return False

    def _promote_cache_entry(self, cache_meta: db.CacheMeta):
        """Warms up accessed cache entry in the bucket."""
        bucket = self._buckets.get_bucket(cache_meta.size)
        bucket.warm_up_entry(cache_meta.hash)

    async def _open_fp_by_cache(self, meta: db.CacheMeta) -> AsyncGenerator:
        """Opens file descriptor by opening local cached entry.

        Args:
            meta CacheMeta: meta for target file that queried from the db.

        Returns:
            An AsyncGenerator that can be yield data chunks from.

        Raises:
            FileNotFoundError: Raised if the file indicates by the meta doesn't exist.
        """
        hash: str = meta.hash
        fpath = self._base_dir / hash

        if fpath.is_file():

            async def _fp():
                async with aiofiles.open(fpath, "rb", executor=self._executor) as f:
                    while True:
                        data = await f.read(self._chunk_size)
                        if len(data) > 0:
                            yield data
                        else:
                            break

            return _fp()
        else:
            raise FileNotFoundError(f"cache entry {hash} doesn't exist!")

    async def _open_fp_by_requests(
        self, raw_url: str, cookies: Dict[str, str], extra_headers: Dict[str, str]
    ) -> Tuple[AsyncGenerator, db.CacheMeta]:
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
        from urllib.parse import quote, urlparse

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

        ###### wrap the request inside a generator ######
        async def _fp():
            async with self._session.get(
                url, proxy=self._upper_proxy, cookies=cookies, headers=extra_headers
            ) as response:
                # assembling output cachemeta
                # NOTE: output cachemeta doesn't have hash and size set yet
                # NOTE.2: store the original unquoted url into the CacheMeta
                yield db.CacheMeta(
                    url=raw_url,
                    hash=None,
                    size=0,
                    content_encoding=response.headers.get("content-encoding", ""),
                    content_type=response.headers.get(
                        "content-type", "application/octet-stream"
                    ),
                )

                async for data, _ in response.content.iter_chunks():
                    yield data

        # start the fp
        res = _fp()
        meta = await res.__anext__()

        # return the generator and the meta
        return res, meta

    # exposed API
    async def retrieve_file(
        self,
        url: str,
        /,
        cookies: Dict[str, str],
        extra_headers: Dict[str, str],
        cache_control_policies: Set[OTAFileCacheControl],
    ) -> OTAFile:
        """Exposed API to retrieve a file descriptor.

        This method retrieves a file descriptor for incoming client request.
        Upper uvicorn app can use this file descriptor to yield chunks of data,
        and stream chunks to the on-calling ota_client.

        Args:
            url str
            cookies Dict[str, str]: cookies in the incoming client request.
            extra_headers Dict[str, str]: headers in the incoming client request.
                Currently Cookies and Authorization headers are used.

        Returns:
            OTAFile: An instance of OTAFile that wraps a file descriptor representing
            the requested resources. It provides get_chunks API for streaming data.
            See documents of OTAFile.get_chunks for details.

        Example usage:
            a. Open a file descriptor and prepare meta data of URL
                by _open_fp_by_cache or _open_fp_by_request.
            b. (If request remote file and store_cache=True) Register the instance's
                background_write_cache method to the thread pool to caching file
                when file is being downloading.
                Also remember to register the callback to commit/flush the cache.
            c. Pass the instance to upper uvicorn app,
                and then app can retrieve sequences of data chunks via get_chunks API.
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
        ):
            fp, meta = await self._open_fp_by_requests(url, cookies, extra_headers)
            return OTAFile(url, meta, fp)

        # cache enabled, lookup the database
        no_cache_available = True
        cache_meta = self._db.lookup_url(url)
        if cache_meta:  # cache hit
            logger.debug(f"cache hit for {url=}\n, {cache_meta=}")

            cache_path: Path = self._base_dir / cache_meta.hash
            # clear the cache entry if the ota_client instructs so
            if retry_cache:
                logger.warning(f"retry_cache: try to clear entry for {cache_meta=}..")
                cache_path.unlink(missing_ok=True)

            if not cache_path.is_file():
                # invalid cache entry found in the db, cleanup it
                logger.error(f"dangling cache entry found: {cache_meta=}")
                self._db.remove_urls(url)
            else:
                no_cache_available = False

        # case 2: no valid cache entry presented, try to cache the requested file
        if no_cache_available:
            logger.debug(f"try to download and cache {url=}")
            fp, meta = await self._open_fp_by_requests(url, cookies, extra_headers)

            # case 2.1: download and cache new file
            if self._on_going_caching.register(url):
                res = OTAFile(
                    url,
                    meta,
                    fp,
                    store_cache=True,
                    below_hard_limit_event=self._storage_below_hard_limit_event,
                )

                # dispatch the background cache writing to executor
                self._executor.submit(
                    res.background_write_cache, self._register_cache_callback
                )
                return res

            # case 2.2: failed to get the chance to cache, still query file from remote
            logger.debug(
                f"failed to get the chance to cache..., directly download {url=}"
            )
            fp, meta = await self._open_fp_by_requests(url, cookies, extra_headers)
            return OTAFile(url, meta, fp)

        # case 3: cache is available and valid, use cache
        logger.debug(f"use cache for {url=}")
        self._promote_cache_entry(cache_meta)

        fp = await self._open_fp_by_cache(cache_meta)
        # use cache
        return OTAFile(url, cache_meta, fp)
