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
import logging
import shutil
import threading
import time
from pathlib import Path
from typing import AsyncIterator, Mapping, Optional
from urllib.parse import SplitResult, quote, urlsplit

import aiohttp
import anyio
import anyio.to_thread
from multidict import CIMultiDict, CIMultiDictProxy

from otaclient_common._typing import StrOrPath
from otaclient_common.common import get_backoff

from ._consts import HEADER_CONTENT_ENCODING, HEADER_OTA_FILE_CACHE_CONTROL
from .cache_control_header import OTAFileCacheControl
from .cache_streaming import CacheTracker, CachingRegister, cache_streaming
from .config import config as cfg
from .db import CacheMeta, check_db, init_db
from .errors import BaseOTACacheError
from .external_cache import mount_external_cache, umount_external_cache
from .lru_cache_helper import LRUCacheHelper
from .utils import read_file, url_based_hash

logger = logging.getLogger(__name__)


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
    _upper_cache_policy = OTAFileCacheControl.parse_header(
        resp_headers_from_upper.get(HEADER_OTA_FILE_CACHE_CONTROL, "")
    )
    if _upper_cache_policy.file_sha256:
        file_sha256 = _upper_cache_policy.file_sha256
        file_compression_alg = _upper_cache_policy.file_compression_alg or None
    else:
        file_sha256 = cache_identifier
        file_compression_alg = compression_alg

    return CacheMeta(
        file_sha256=file_sha256,
        file_compression_alg=file_compression_alg,
        url=raw_url,
        content_encoding=resp_headers_from_upper.get(HEADER_CONTENT_ENCODING),
    )


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
        base_dir: Optional[StrOrPath] = None,
        db_file: Optional[StrOrPath] = None,
        upper_proxy: str = "",
        enable_https: bool = False,
        external_cache_mnt_point: str | None = None,
    ):
        """Init ota_cache instance with configurations."""
        logger.info(
            f"init ota_cache({cache_enabled=}, {init_cache=}, {upper_proxy=}, {enable_https=})"
        )
        self._closed = True
        self._shutdown_lock = asyncio.Lock()

        self.table_name = table_name = cfg.TABLE_NAME
        self._chunk_size = cfg.CHUNK_SIZE
        self._cache_enabled = cache_enabled
        self._init_cache = init_cache
        self._enable_https = enable_https

        self._base_dir = Path(base_dir) if base_dir else Path(cfg.BASE_DIR)
        self._db_file = db_f = Path(db_file) if db_file else Path(cfg.DB_FILE)

        self._base_dir.mkdir(parents=True, exist_ok=True)
        if not check_db(self._db_file, table_name):
            logger.info(f"db file is broken, force init db file at {db_f}")
            db_f.unlink(missing_ok=True)
            self._init_cache = True  # force init cache on db file cleanup

        self._external_cache_data_dir = None
        self._external_cache_mp = None
        if external_cache_mnt_point and mount_external_cache(external_cache_mnt_point):
            logger.info(
                f"external cache source is enabled at: {external_cache_mnt_point}"
            )
            self._external_cache_mp = external_cache_mnt_point
            self._external_cache_data_dir = (
                anyio.Path(external_cache_mnt_point) / cfg.EXTERNAL_CACHE_DATA_DNAME
            )

        self._storage_below_hard_limit_event = threading.Event()
        self._storage_below_soft_limit_event = threading.Event()
        self._upper_proxy = upper_proxy

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
            if self._init_cache:
                logger.warning("purge and init ota_cache")
                shutil.rmtree(str(self._base_dir), ignore_errors=True)
                self._base_dir.mkdir(exist_ok=True, parents=True)
                # init db file with table created
                self._db_file.unlink(missing_ok=True)

                init_db(self._db_file, cfg.TABLE_NAME)

            # reuse the previously left ota_cache
            else:  # cleanup unfinished tmp files
                async for tmp_f in anyio.Path(self._base_dir).glob(
                    f"{cfg.TMP_FILE_PREFIX}*"
                ):
                    await tmp_f.unlink(missing_ok=True)

            # dispatch a background task to pulling the disk usage info
            _free_space_check_thread = threading.Thread(
                target=self._background_check_free_space,
                daemon=True,
                name="ota_cache_free_space_checker",
            )
            _free_space_check_thread.start()

            # init cache helper(and connect to ota_cache db)
            self._lru_helper = LRUCacheHelper(
                self._db_file,
                bsize_dict=cfg.BUCKET_FILE_SIZE_DICT,
                table_name=cfg.TABLE_NAME,
                thread_nums=cfg.DB_THREADS,
                thread_wait_timeout=cfg.DB_THREAD_WAIT_TIMEOUT,
            )
            self._on_going_caching = CachingRegister()

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
        async with self._shutdown_lock:
            if not self._closed:
                self._closed = True
                await self._session.close()

                if self._cache_enabled:
                    self._lru_helper.close()

                if self._external_cache_mp:
                    umount_external_cache(self._external_cache_mp)

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

            current_used_p = int(disk_usage.used / disk_usage.total * 100)

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
                    f"disk usage reached hard limit({current_used_p=}%,"
                    f"{cfg.DISK_USE_LIMIT_HARD_P=}%), cache disabled"
                )
            elif not _previous_below_hard and _current_below_hard:
                logger.info(
                    f"disk usage is below hard limit({current_used_p=}%),"
                    f"{cfg.DISK_USE_LIMIT_SOFT_P=}%), cache enabled again"
                )
            time.sleep(cfg.DISK_USE_PULL_INTERVAL)

    def _cache_entries_cleanup(self, entry_hashes: list[str]) -> None:
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
            await anyio.to_thread.run_sync(self._cache_entries_cleanup, _hashes)
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
    ) -> tuple[AsyncIterator[bytes], CIMultiDictProxy[str]]:
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

    async def _retrieve_file_by_cache_lookup(
        self, *, raw_url: str, cache_policy: OTAFileCacheControl
    ) -> tuple[AsyncIterator[bytes], CIMultiDict[str]] | None:
        """
        Returns:
            A tuple of bytes iterator and headers dict for back to client.
        """
        if (
            not self._cache_enabled
            or cache_policy.no_cache
            or cache_policy.retry_caching
        ):
            return

        cache_identifier = cache_policy.file_sha256
        if not cache_identifier:
            # fallback to use URL based hash, and clear compression_alg for such case
            cache_identifier = url_based_hash(raw_url)

        meta_db_entry = await self._lru_helper.lookup_entry(cache_identifier)
        if not meta_db_entry:
            return

        # NOTE: db_entry.file_sha256 can be either
        #           1. valid sha256 value for corresponding plain uncompressed OTA file
        #           2. URL based sha256 value for corresponding requested URL
        cache_file = anyio.Path(self._base_dir / cache_identifier)

        # check if cache file exists
        # NOTE(20240729): there is an edge condition that the finished cached file is not yet renamed,
        #   but the database entry has already been inserted. Here we wait for 3 rounds for
        #   cache_commit_callback to rename the tmp file.
        _retry_count_max, _factor, _backoff_max = 6, 0.01, 0.1  # 0.255s in total
        for _retry_count in range(_retry_count_max):
            if await cache_file.is_file():
                break
            await asyncio.sleep(get_backoff(_retry_count, _factor, _backoff_max))

        if not await cache_file.is_file():
            logger.warning(
                f"dangling cache entry found, remove db entry: {meta_db_entry}"
            )
            await self._lru_helper.remove_entry(cache_identifier)
            return

        # NOTE: we don't verify the cache here even cache is old, but let otaclient's hash verification
        #       do the job. If cache is invalid, otaclient will use CacheControlHeader's retry_cache
        #       directory to indicate invalid cache.
        return (
            read_file(cache_file),
            meta_db_entry.export_headers_to_client(),
        )

    async def _retrieve_file_by_external_cache(
        self, client_cache_policy: OTAFileCacheControl
    ) -> tuple[AsyncIterator[bytes], CIMultiDict[str]] | None:
        # skip if not external cache or otaclient doesn't sent valid file_sha256
        if (
            not self._external_cache_data_dir
            or client_cache_policy.no_cache
            or client_cache_policy.retry_caching
            or not client_cache_policy.file_sha256
        ):
            return

        cache_identifier = client_cache_policy.file_sha256
        cache_file = self._external_cache_data_dir / cache_identifier
        cache_file_zst = anyio.Path(
            cache_file.with_suffix(f".{cfg.EXTERNAL_CACHE_STORAGE_COMPRESS_ALG}")
        )

        if await cache_file_zst.is_file():
            _header = CIMultiDict()
            _header[HEADER_OTA_FILE_CACHE_CONTROL] = (
                OTAFileCacheControl.export_kwargs_as_header(
                    file_sha256=cache_identifier,
                    file_compression_alg=cfg.EXTERNAL_CACHE_STORAGE_COMPRESS_ALG,
                )
            )
            return read_file(cache_file_zst), _header

        if await cache_file.is_file():
            _header = CIMultiDict()
            _header[HEADER_OTA_FILE_CACHE_CONTROL] = (
                OTAFileCacheControl.export_kwargs_as_header(
                    file_sha256=cache_identifier
                )
            )
            return read_file(cache_file), _header

    async def _retrieve_file_by_new_caching(
        self,
        *,
        raw_url: str,
        cache_policy: OTAFileCacheControl,
        headers_from_client: dict[str, str],
    ) -> tuple[AsyncIterator[bytes], CIMultiDictProxy[str] | CIMultiDict[str]] | None:
        # NOTE(20241202): no new cache on hard limit being reached
        if (
            not self._cache_enabled
            or cache_policy.no_cache
            or not self._storage_below_hard_limit_event.is_set()
        ):
            return

        cache_identifier = cache_policy.file_sha256
        compression_alg = cache_policy.file_compression_alg
        if not cache_identifier:
            # fallback to use URL based hash, and clear compression_alg for such case
            cache_identifier = url_based_hash(raw_url)
            compression_alg = ""

        # if set, cleanup any previous cache file before starting new cache
        if cache_policy.retry_caching:
            logger.debug(f"requested with retry_cache for {raw_url=} ...")
            await self._lru_helper.remove_entry(cache_identifier)
            (self._base_dir / cache_identifier).unlink(missing_ok=True)

        if (tracker := self._on_going_caching.get_tracker(cache_identifier)) and (
            subscription := await tracker.subscribe_tracker()
        ):
            # logger.debug(f"reader subscribe for {tracker.meta=}")
            stream_fd, cache_meta = subscription
            return stream_fd, cache_meta.export_headers_to_client()

        # no valid online tracker is available for this request, create a new one and
        #   promote the caller to be the provider.
        # NOTE: register the tracker before open the remote fd!
        tracker = CacheTracker(
            cache_identifier=cache_identifier,
            base_dir=self._base_dir,
            commit_cache_cb=self._commit_cache_callback,
            below_hard_limit_event=self._storage_below_hard_limit_event,
        )
        self._on_going_caching.register_tracker(cache_identifier, tracker)

        # caller is the provider of the requested resource
        try:
            remote_fd, resp_headers = await self._retrieve_file_by_downloading(
                raw_url, headers=headers_from_client
            )
            cache_meta = create_cachemeta_for_request(
                raw_url,
                cache_identifier,
                compression_alg,
                resp_headers_from_upper=resp_headers,
            )
            # start caching
            wrapped_fd = cache_streaming(remote_fd, tracker, cache_meta)
            return wrapped_fd, resp_headers
        except Exception:
            tracker.set_writer_failed()
            raise
        finally:
            tracker = None  # remove ref

    # exposed API

    async def retrieve_file(
        self, raw_url: str, headers_from_client: dict[str, str]
    ) -> tuple[AsyncIterator[bytes], CIMultiDict[str] | CIMultiDictProxy[str]] | None:
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

        # when there is no upper_proxy, do not passthrough the OTA_FILE_CACHE_CONTROL header.
        if not self._upper_proxy:
            headers_from_client.pop(HEADER_OTA_FILE_CACHE_CONTROL, None)

        # a fastpath when cache is not enabled or client requires so
        if not self._cache_enabled or cache_policy.no_cache:
            return await self._retrieve_file_by_downloading(
                raw_url, headers=headers_from_client
            )

        # NOTE(20241202): behavior changed: even if _cache_enabled is False, if external_cache is configured
        #   and loaded, still try to use external cache source.
        if _res := await self._retrieve_file_by_external_cache(cache_policy):
            return _res

        if _res := await self._retrieve_file_by_cache_lookup(
            raw_url=raw_url, cache_policy=cache_policy
        ):
            return _res

        if _res := await self._retrieve_file_by_new_caching(
            raw_url=raw_url,
            cache_policy=cache_policy,
            headers_from_client=headers_from_client,
        ):
            return _res

        # as last resort, finally try to handle the request by directly downloading
        return await self._retrieve_file_by_downloading(
            raw_url, headers=headers_from_client
        )
