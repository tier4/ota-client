import asyncio
import requests
import subprocess
import shlex
import shutil
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from hashlib import sha256
from threading import Lock, Event
from typing import Dict
from pathlib import Path

from . import db

import logging

logger = logging.getLogger(__name__)


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


class _Bucket(OrderedDict):
    def __init__(self, size: int, cache_dir: str):
        super().__init__()
        self._lock = Lock()
        self._base = cache_dir
        self.size = size

    def add_entry(self, key):
        """
        newly added item will be added to the last(right)
        this method can also be used to warm up an entry
        """
        self[key] = None
        self.move_to_end(key)

    warm_up_entry = add_entry

    def popleft(self) -> str:
        try:
            res, _ = self.popitem(last=False)
            return res
        except KeyError:
            return

    def reserve_space(self, size: int) -> list:
        enough_space = False
        with self._lock:
            hash_list, files_list = [], []
            space_available = None
            for h in self:
                if space_available >= size:
                    break
                else:
                    f: Path = Path(self._base) / h
                    if f.is_file():
                        # TODO: deal with dangling cache?
                        space_available += f.stat().st_size
                        hash_list.append(h)
                        files_list.append(f)

            if space_available >= size:
                enough_space = True
                # we can reserve enough space from current bucket
                for h in hash_list:
                    del self[h]

        if enough_space:
            for f in files_list:
                f.unlink(missing_ok=True)
            return hash_list

        return


class OTAFile:
    CHUNK_SIZE = 2097_152  # in bytes, 2MB

    def __init__(
        self,
        url: str,
        base_path: str,
        session: requests.Session,
        meta: db.CacheMeta = None,
        store_cache: bool = False,
        enable_https: bool = False,
        free_space_event: Event = None,
    ):
        logger.debug(f"new OTAFile request: {url}")
        self.base_path = base_path
        self._session = session
        self._cached = False
        self._store_cache = store_cache
        self._free_space_event = free_space_event

        # cache entry meta
        self.url = url
        self.hash = None
        self.content_type = None
        self.content_encoding = None
        self.size = 0

        # data fp that we used in iterator
        self._fp = None
        self._dst_fp = None  # cache entry dst if store_cache
        self._response = None

        # life cycle
        self._finished = False
        self.cached_success = False

        if meta:  # cache entry presented
            self.hash: str = meta.hash
            self.fpath = Path(self.base_path) / self.hash

            if self.fpath.is_file():
                # check if it is a dangling cache
                # NOTE: we are currently not dealing with dangling entry yet
                self.content_type = meta.content_type
                self.content_encoding = meta.content_encoding
                self.size = meta.size
                self._cached = True
                self._fp = open(self.fpath, "rb")

        if not self._cached:
            self._queue = Queue()

            # open the remote connection
            _url = url
            if enable_https:
                _url = url.replace("http", "https")

            self._response = self._session.get(_url, stream=True)
            self.content_type = self._response.headers.get(
                "Content-Type", "application/octet-stream"
            )
            self.content_encoding = self._response.headers.get("Content-Encoding", "")
            self._fp = self._response.raw

    def background_write_cache(self, callback):
        if not self._store_cache or not self._free_space_event:
            return

        try:
            logger.debug(f"start to cache for {self.url}...")
            self._hash_f = sha256()
            tmp_fpath = Path(self.base_path) / str(time.time()).replace(".", "")
            self.temp_fpath = tmp_fpath
            self._dst_fp = open(tmp_fpath, "wb")

            while not self._finished or not self._queue.empty():
                if not self._free_space_event.is_set():
                    # if the free space is not enough duing caching
                    # abort the caching
                    logger.error(
                        f"not enough free space during caching url={self.url}, abort"
                    )
                    break

                try:
                    data = self._queue.get(timeout=360)
                except Exception:
                    logger.error(f"timeout caching for {self.url}, abort")
                    break
                self._hash_f.update(data)
                self.size += self._dst_fp.write(data)

            self._dst_fp.close()
            if self._finished and self.size > 0:  # not caching 0 size file
                # rename the file to the hash value
                self.hash = self._hash_f.hexdigest()[:16]
                self.temp_fpath.rename(Path(self.base_path) / self.hash)
                self.cached_success = True
                logger.debug(f"successfully cache {self.url}")
            # NOTE: if queue is empty but self._finished is not set
            # an unfinished caching might happen
        finally:
            # execute the callback function
            callback(self)

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if self._finished:
            raise ValueError("file is closed")

        chunk = self._fp.read(self.CHUNK_SIZE)
        if len(chunk) == 0:  # stream finished
            self._finished = True
            # finish the background cache writing
            if self._store_cache:
                self._queue.put(b"")

            # cleanup
            self._fp.close()
            if self._response is not None:
                self._response.close()

            raise StopAsyncIteration
        else:
            # stream the contents to background caching task non-blockingly
            if self._store_cache:
                self._queue.put_nowait(chunk)

            return chunk


class OTACache:
    CACHE_DIR = "/ota-cache"
    DB_FILE = f"{CACHE_DIR}/cache_db"
    DISK_USE_LIMIT_P = 70  # in p%
    DISK_USE_PULL_INTERVAL = 2  # in seconds
    BUCKET_FILE_SIZE_LIST = (
        0,
        10_240,  # 10KiB
        102_400,  # 100KiB
        512_000,  # 500KiB
        1_048_576,  # 1MiB
        5_242_880,  # 5MiB
        10_485_760,  # 10MiB
        104_857_600,  # 100MiB
        1_048_576_000,  # 1GiB
    )  # Bytes

    def __init__(
        self,
        cache_enabled: bool,
        init: bool,
        upper_proxy: str = None,
        enable_https: bool = False,
    ):
        logger.debug(f"init ota cache({cache_enabled=}, {init=}, {upper_proxy=})")

        self._closed = False
        self._cache_enabled = cache_enabled
        self._enable_https = enable_https
        self._executor = ThreadPoolExecutor()

        self._enough_free_space_event = Event()

        if cache_enabled:
            self._cache_enabled = True

            # prepare cache dire
            if init:
                shutil.rmtree(self.CACHE_DIR, ignore_errors=True)
            Path(self.CACHE_DIR).mkdir(exist_ok=True, parents=True)

            # dispatch a background task to pulling the disk usage info
            self._executor.submit(self._background_check_free_space)

            self._db = db.OTACacheDB(self.DB_FILE)
            # NOTE: requests doesn't decompress the contents,
            # we cache the contents as its original form, and send
            # to the client
            self._session = requests.Session()
            if upper_proxy:
                # override the proxy setting if we configure one
                proxies = {"http": upper_proxy, "https": ""}
                self._session.proxies.update(proxies)
                # if upper proxy presented, we must disable https
                self._enable_https = False

            self._init_buckets()
        else:
            self._cache_enabled = False

    def close(self, cleanup: bool = False):
        logger.debug(f"shutdown ota-cache({cleanup=})...")
        if self._cache_enabled and not self._closed:
            self._closed = True
            self._executor.shutdown(wait=True)
            self._db.close()

            if cleanup:
                shutil.rmtree(self.CACHE_DIR, ignore_errors=True)
        logger.info("shutdown ota-cache completed")

    def _background_check_free_space(self) -> bool:
        while not self._closed:
            try:
                cmd = f"df --output=pcent {self.CACHE_DIR}"
                current_used_p = _subprocess_check_output(cmd, raise_exception=True)

                # expected output:
                # 0: Use%
                # 1: 33%
                current_used_p = int(current_used_p.splitlines()[-1].strip(" %"))
                if current_used_p < self.DISK_USE_LIMIT_P:
                    self._enough_free_space_event.set()
                else:
                    self._enough_free_space_event.clear()
            except Exception:
                self._enough_free_space_event.clear()

            time.sleep(self.DISK_USE_PULL_INTERVAL)

    def _init_buckets(self):
        self._buckets: Dict[_Bucket] = dict()  # dict[file_size_target]_Bucket

        for s in self.BUCKET_FILE_SIZE_LIST:
            self._buckets[s] = _Bucket(size=s, cache_dir=self.CACHE_DIR)

    def _register_cache_callback(self, f: OTAFile):
        """
        the callback for finishing up caching

        NOTE:
        1. if the free space is used up duing caching,
        the caching will terminate immediately.
        2. if the free space is used up after cache writing finished,
        we will first try reserving free space, if fails,
        then we delete the already cached file.
        """
        if f.cached_success and self._ensure_free_space(f.size):
            logger.debug(f"commit cache for {f.url}...")
            bs = self._find_target_bucket_size(f.size)
            self._buckets[bs].add_entry(f.hash)

            # register to the database
            cache_meta = db.CacheMeta(
                url=f.url,
                hash=f.hash,
                size=f.size,
                content_type=f.content_type,
                content_encoding=f.content_encoding,
            )
            self._db.insert_urls(cache_meta)
        else:
            # try to cleanup the dangling cache file
            Path(f.temp_fpath).unlink(missing_ok=True)

    def _find_target_bucket_size(self, file_size: int) -> int:
        if file_size < 0:
            raise ValueError(f"invalid file size {file_size}")

        s, e = 0, len(self.BUCKET_FILE_SIZE_LIST) - 1
        target_size = None

        if file_size >= self.BUCKET_FILE_SIZE_LIST[-1]:
            target_size = self.BUCKET_FILE_SIZE_LIST[-1]
        else:
            idx = None
            while True:
                if abs(e - s) <= 1:
                    idx = s
                    break

                if file_size <= self.BUCKET_FILE_SIZE_LIST[(s + e) // 2]:
                    e = (s + e) // 2
                elif file_size > self.BUCKET_FILE_SIZE_LIST[(s + e) // 2]:
                    s = (s + e) // 2
            target_size = self.BUCKET_FILE_SIZE_LIST[idx]

        return target_size

    def _ensure_free_space(self, size: int) -> bool:
        if not self._enough_free_space_event.is_set():  # no enough free space
            bs = self._find_target_bucket_size(size)
            bucket: _Bucket = self._buckets[bs]

            # first check the current bucket
            hash_list = bucket.reserve_space(size)
            if hash_list:
                self._db.remove_url_by_hash(*hash_list)
                return True

            else:  # if current bucket is not enough, check higher bucket
                entry_to_clear = None
                for bs in self.BUCKET_FILE_SIZE_LIST[
                    self.BUCKET_FILE_SIZE_LIST.index(bs) + 1 :
                ]:
                    bucket = self._buckets[bs]
                    entry_to_clear = bucket.popleft()

                if entry_to_clear:
                    # get one entry from the target bucket
                    # and then delete it

                    f: Path = Path(self.CACHE_DIR) / entry_to_clear
                    f.unlink(missing_ok=True)
                    self._db.remove_url_by_hash(entry_to_clear)

                    return True

        else:  # there is already enough space
            return True

        return False

    def _promote_cache_entry(self, cache_meta: db.CacheMeta):
        bs = self._find_target_bucket_size(cache_meta.size)
        bucket: _Bucket = self._buckets[bs]
        bucket.warm_up_entry(cache_meta.hash)

    # exposed API
    async def retrieve_file(self, url: str) -> OTAFile:
        logger.debug(f"try to retrieve file from {url=}")
        if self._closed:
            raise ValueError("ota cache pool is closed")

        res = None
        if not self._cache_enabled:
            # case 1: not using cache, directly download file
            res = OTAFile(
                url=url,
                base_path=None,
                session=self._session,
                store_cache=False,
                enable_https=self._enable_https,
            )
        else:
            no_cache_available = True

            cache_meta = self._db.lookup_url(url)
            if cache_meta:  # cache hit
                logger.debug(f"cache hit for {url=}\n, {cache_meta=}")
                path = Path(self.CACHE_DIR) / cache_meta.hash
                if not path.is_file():
                    # invalid cache entry found in the db, cleanup it
                    logger.error(f"dangling cache entry found: \n{cache_meta=}")
                    self._db.remove_urls(url)
                    no_cache_available = True
                else:
                    no_cache_available = False
            else:
                no_cache_available = True

            # check whether we should use cache, not use cache,
            # or download and cache the new file
            if no_cache_available:
                # case 2: download and cache new file
                # try to ensure space for new cache
                logger.debug(f"try to download and cache {url=}")
                res = OTAFile(
                    url=url,
                    base_path=self.CACHE_DIR,
                    session=self._session,
                    store_cache=True,
                    enable_https=self._enable_https,
                    free_space_event=self._enough_free_space_event,
                )

                # dispatch the background cache writing to executor
                loop = asyncio.get_running_loop()
                loop.run_in_executor(
                    self._executor,
                    res.background_write_cache,
                    self._register_cache_callback,
                )
            else:
                # case 3: use cache
                # warm up the cache
                logger.debug(f"use cache for {url=}")
                self._promote_cache_entry(cache_meta)
                # use cache
                res = OTAFile(
                    url=url,
                    base_path=self.CACHE_DIR,
                    session=self._session,
                    meta=cache_meta,
                    enable_https=self._enable_https,
                    free_space_event=self._enough_free_space_event,
                )

        return res
