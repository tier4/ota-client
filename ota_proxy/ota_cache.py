import asyncio
from functools import partial
import aiohttp
import subprocess
import shlex
import shutil
import time
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from queue import Queue
from hashlib import sha256
from threading import Lock, Event
from typing import Dict, List, Union, Tuple, BinaryIO
from pathlib import Path
from os import urandom

from . import db
from .config import config as cfg

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


class _Bucket(OrderedDict):
    def __init__(self, size: int):
        super().__init__()
        self._base_dir = Path(cfg.BASE_DIR)
        self._lock = Lock()
        self.size = size

    def add_entry(self, key):
        """
        newly added item will be added to the last(right)
        this method can also be used to warm up an entry
        """
        self[key] = None

    def warm_up_entry(self, key):
        try:
            self.move_to_end(key)
        except KeyError:
            return

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
            space_available = 0
            for h in self:
                if space_available >= size:
                    break
                else:
                    f: Path = Path(self._base_dir) / h
                    if f.is_file():
                        space_available += f.stat().st_size
                        hash_list.append(h)
                        files_list.append(f)
                    else:
                        # deal with dangling cache by telling the caller also
                        # delete the dangling entry from the database
                        hash_list.append(h)
                        logger.warning(f"dangling cache entry found: {h}")

            if space_available >= size:
                enough_space = True
                # we can reserve enough space from current bucket
                for h in hash_list:
                    del self[h]

        if enough_space:
            for f in files_list:
                f.unlink(missing_ok=True)
            return hash_list


class OTAFile:
    def __init__(
        self,
        url: str,
        fp: Union[aiohttp.ClientResponse, BinaryIO],
        meta: db.CacheMeta,
        store_cache: bool = False,
        below_hard_limit_event: Event = None,
    ):
        logger.debug(f"new OTAFile request: {url}")
        self._base_dir = Path(cfg.BASE_DIR)
        self._store_cache = store_cache
        self._storage_below_hard_limit = below_hard_limit_event
        # NOTE: for new cache entry meta, the hash and size are not set yet
        self.meta = meta

        # data stream
        self._remote = False
        if isinstance(fp, aiohttp.ClientResponse):
            self._remote = True

        self._fp = fp
        self._queue = None

        # life cycle
        self.finished = False
        self.cached_success = False

        # prepare for data streaming
        if store_cache:
            self._hash_f = sha256()
            self._queue = Queue()

    def background_write_cache(self, callback):
        if not self._store_cache or not self._storage_below_hard_limit:
            # call callback function even we don't cache anything
            # as we need to cleanup the status
            callback(self)
            return

        try:
            logger.debug(f"start to cache for {self.meta.url}...")
            self.temp_fpath = self._base_dir / f"tmp_{urandom(16).hex()}"
            self._dst_fp = open(self.temp_fpath, "wb")

            while not self.finished or not self._queue.empty():
                if not self._storage_below_hard_limit.is_set():
                    # reach storage hard limit, abort caching
                    logger.debug(
                        f"not enough free space during caching url={self.meta.url}, abort"
                    )
                    break

                try:
                    data = self._queue.get(timeout=360)
                except Exception:
                    logger.error(f"timeout caching for {self.meta.url}, abort")
                    break

                self._hash_f.update(data)
                self.meta.size += self._dst_fp.write(data)

            self._dst_fp.close()
            if self.finished and self.meta.size > 0:  # not caching 0 size file
                # rename the file to the hash value
                hash = self._hash_f.hexdigest()

                self.meta.hash = hash
                self.temp_fpath.rename(self._base_dir / hash)
                self.cached_success = True
                logger.debug(f"successfully cache {self.meta.url}")
            # NOTE: if queue is empty but self._finished is not set
            # an unfinished caching might happen
        finally:
            # commit the cache via callback
            self._dst_fp.close()
            callback(self)

    def __aiter__(self):
        return self

    async def _get_chunk(self) -> bytes:
        if self._remote:
            return await self._fp.content.read(cfg.CHUNK_SIZE)
        else:
            return self._fp.read(cfg.CHUNK_SIZE)

    async def __anext__(self) -> bytes:
        if self.finished:
            raise ValueError("file is closed")

        chunk = await self._get_chunk()
        if len(chunk) == 0:  # stream finished
            self.finished = True
            # finish the background cache writing
            if self._store_cache:
                self._queue.put_nowait(b"")

            # cleanup
            if not self._remote:
                self._fp.close()
            else:
                self._fp.release()

            raise StopAsyncIteration
        else:
            # stream the contents to background caching task non-blockingly
            if self._store_cache:
                self._queue.put_nowait(chunk)

            return chunk


class OTACacheHelper:
    """
    a helper bundle to maintain ota-cache
    """

    def __init__(self, event: Event):
        self._base_dir = Path(cfg.BASE_DIR)
        self._db = db.OTACacheDB(cfg.DB_FILE)

        self._event = event

    @staticmethod
    def _check_entry(base_dir: str, meta: db.CacheMeta) -> List[db.CacheMeta, bool]:
        f = Path(base_dir) / meta.hash
        if f.is_file():
            hash_f = sha256()
            # calculate file's hash and check against meta
            with open(f, "rb") as fp:
                while True:
                    data = fp.read(cfg.CHUNK_SIZE)
                    if data:
                        hash_f.update(data)
                    else:
                        break

            if hash_f.hexdigest() == meta.hash:
                return meta, True

        # check failed, try to remove the cache entry
        f.unlink(missing_ok=True)
        return meta, False

    def scrub_cache(self):
        from os import cpu_count

        logger.debug("start to scrub the cache entries...")

        if self._event.is_set():
            self._event.clear()

        dangling_db_entry = []
        # NOTE: pre-add database file into the set
        # to prevent db file being deleted
        valid_cache_entry = {Path(cfg.DB_FILE).name}
        with ProcessPoolExecutor(max_workers=cpu_count * 2 // 3) as pool:
            res_list = pool.map(
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

        self._event.set()
        logger.debug("scrub finished")


class OTACache:
    def __init__(
        self,
        cache_enabled: bool,
        init: bool,
        upper_proxy: str = None,
        enable_https: bool = False,
    ):
        logger.debug(f"init ota cache({cache_enabled=}, {init=}, {upper_proxy=})")

        self._base_dir = Path(cfg.BASE_DIR)
        self._closed = False
        self._cache_enabled = cache_enabled
        self._enable_https = enable_https
        self._executor = ThreadPoolExecutor()

        self._storage_below_hard_limit_event = Event()
        self._storage_below_soft_limit_event = Event()
        self._on_going_caching = dict()
        self._upper_proxy: str = ""

        _event = Event()
        self._scrub_finished_event = _event
        self._cache_helper = OTACacheHelper(_event)

        if cache_enabled:
            self._cache_enabled = True

            # prepare cache dire
            if init:
                shutil.rmtree(str(self._base_dir), ignore_errors=True)
                self._base_dir.mkdir(exist_ok=True, parents=True)
            else:
                # scrub the cache folder in the background
                self._base_dir.mkdir(exist_ok=True, parents=True)
                self._executor.submit(self._cache_helper.scrub_cache)

            # dispatch a background task to pulling the disk usage info
            self._executor.submit(self._background_check_free_space)

            # connect to db
            self._db = db.OTACacheDB(cfg.DB_FILE)

            if upper_proxy:
                # if upper proxy presented, we must disable https
                self._upper_proxy = upper_proxy
                self._enable_https = False

            # NOTE: we configure aiohttp to not decompress the contents,
            # we cache the contents as its original form, and send
            # to the client with proper headers to indicate the client to
            # compress the payload by their own
            self._session = aiohttp.ClientSession(
                auto_decompress=False, raise_for_status=True
            )

            self._init_buckets()
        else:
            self._cache_enabled = False

    def close(self):
        logger.debug("shutdown ota-cache...")
        if self._cache_enabled and not self._closed:
            self._closed = True
            self._executor.shutdown(wait=True)
            self._db.close()

        logger.info("shutdown ota-cache completed")

    def _background_check_free_space(self):
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

    def _init_buckets(self):
        self._buckets: Dict[_Bucket] = dict()  # dict[file_size_target]_Bucket

        for s in cfg.BUCKET_FILE_SIZE_LIST:
            self._buckets[s] = _Bucket(s)

    def _commit_cache(self, m: db.CacheMeta):
        logger.debug(f"commit cache for {m.url}...")
        bs = self._find_target_bucket_size(m.size)
        self._buckets[bs].add_entry(m.hash)

        # register to the database
        self._db.insert_urls(m)

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
            logger.debug(f"cache for {meta.url=} failed, cleanup")
            Path(f.temp_fpath).unlink(missing_ok=True)

        # always remember to remove url in the on_going_cache_list!
        del self._on_going_caching[meta.url]

    def _find_target_bucket_size(self, file_size: int) -> int:
        if file_size < 0:
            raise ValueError(f"invalid file size {file_size}")

        s, e = 0, len(cfg.BUCKET_FILE_SIZE_LIST) - 1
        target_size = None

        if file_size >= cfg.BUCKET_FILE_SIZE_LIST[-1]:
            target_size = cfg.BUCKET_FILE_SIZE_LIST[-1]
        else:
            idx = None
            while True:
                if abs(e - s) <= 1:
                    idx = s
                    break

                if file_size <= cfg.BUCKET_FILE_SIZE_LIST[(s + e) // 2]:
                    e = (s + e) // 2
                elif file_size > cfg.BUCKET_FILE_SIZE_LIST[(s + e) // 2]:
                    s = (s + e) // 2
            target_size = cfg.BUCKET_FILE_SIZE_LIST[idx]

        return target_size

    def _ensure_free_space(self, size: int) -> bool:
        bs = self._find_target_bucket_size(size)
        bucket: _Bucket = self._buckets[bs]

        # first check the current bucket
        hash_list = bucket.reserve_space(size)
        if hash_list:
            self._db.remove_url_by_hash(*hash_list)
            return True

        else:  # if current bucket is not enough, check higher bucket
            entry_to_clear = None
            for bs in cfg.BUCKET_FILE_SIZE_LIST[
                cfg.BUCKET_FILE_SIZE_LIST.index(bs) + 1 :
            ]:
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
        bs = self._find_target_bucket_size(cache_meta.size)
        bucket: _Bucket = self._buckets[bs]
        bucket.warm_up_entry(cache_meta.hash)

    def _open_fp_by_cache(self, meta: db.CacheMeta) -> BinaryIO:
        hash: str = meta.hash
        fpath = self._base_dir / hash

        if fpath.is_file():
            return open(fpath, "rb")

    async def _open_fp_by_requests(
        self, raw_url: str, cookies: Dict[str, str], extra_headers: Dict[str, str]
    ) -> Tuple[aiohttp.ClientResponse, db.CacheMeta]:
        from urllib.parse import quote, urlparse

        url_parsed = urlparse(raw_url)

        # NOTE: raw_url is unquoted, we must quote it again before we send it to the remote
        url_parsed = url_parsed._replace(path=quote(url_parsed.path))
        if self._enable_https:
            url_parsed = url_parsed._replace(scheme="https")

        url = url_parsed.geturl()

        response = await self._session.get(
            url, proxy=self._upper_proxy, cookies=cookies, headers=extra_headers
        )
        response.raise_for_status()

        # assembling output cachemeta
        # NOTE: output cachemeta doesn't have hash and size set yet
        # NOTE.2: store the original unquoted url into the CacheMeta
        meta = db.CacheMeta(
            url=raw_url, hash=None, content_encoding=None, content_type=None, size=0
        )

        meta.content_type = response.headers.get(
            "content-type", "application/octet-stream"
        )
        meta.content_encoding = response.headers.get("content-encoding", "")

        # return the raw response and a new CacheMeta instance
        return response, meta

    # exposed API
    async def retrieve_file(
        self, url: str, cookies: Dict[str, str], extra_headers: Dict[str, str]
    ) -> OTAFile:
        if self._closed:
            raise ValueError("ota cache pool is closed")

        res = None
        # NOTE: also check if there is already an on-going caching
        # NOTE.2: disable caching when scrubbing is on-going
        if (
            not self._cache_enabled
            or url in self._on_going_caching
            or not self._scrub_finished_event.is_set()
        ):
            # case 1: not using cache, directly download file
            fp, meta = await self._open_fp_by_requests(url, cookies, extra_headers)
            res = OTAFile(url=url, fp=fp, meta=meta)
        else:
            no_cache_available = True

            cache_meta = self._db.lookup_url(url)
            if cache_meta:  # cache hit
                logger.debug(f"cache hit for {url=}\n, {cache_meta=}")
                path = self._base_dir / cache_meta.hash
                if not path.is_file():
                    # invalid cache entry found in the db, cleanup it
                    logger.error(f"dangling cache entry found: \n{cache_meta=}")
                    self._db.remove_urls(url)
                else:
                    no_cache_available = False

            # check whether we should use cache, not use cache,
            # or download and cache the new file
            if no_cache_available:
                # case 2: download and cache new file
                logger.debug(f"try to download and cache {url=}")
                fp, meta = await self._open_fp_by_requests(url, cookies, extra_headers)
                # NOTE: remember to remove the url after cache comitted!
                self._on_going_caching[url] = None

                res = OTAFile(
                    url=url,
                    fp=fp,
                    meta=meta,
                    store_cache=True,
                    below_hard_limit_event=self._storage_below_hard_limit_event,
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
                logger.debug(f"use cache for {url=}")
                self._promote_cache_entry(cache_meta)

                fp = self._open_fp_by_cache(cache_meta)
                # use cache
                res = OTAFile(url=url, fp=fp, meta=cache_meta)

        return res
