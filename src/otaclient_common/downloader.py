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
"""A common used downloader implementation for otaclient."""


from __future__ import annotations

import errno
import logging
import os
import threading
import time
from abc import abstractmethod
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import wraps
from hashlib import sha256
from os import PathLike
from typing import (
    IO,
    Any,
    ByteString,
    Callable,
    Dict,
    Iterator,
    Mapping,
    Optional,
    Protocol,
    Tuple,
    Union,
)
from urllib.parse import urlsplit

import requests
import requests.exceptions
import zstandard
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict as CIDict
from typing_extensions import ParamSpec, TypeVar
from urllib3.response import HTTPResponse
from urllib3.util.retry import Retry

from ota_proxy import OTAFileCacheControl
from otaclient.app.configs import config as cfg
from otaclient_common import copy_callable_typehint
from otaclient_common.common import wait_with_backoff

logger = logging.getLogger(__name__)

EMPTY_FILE_SHA256 = r"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
CACHE_CONTROL_HEADER = OTAFileCacheControl.HEADER_LOWERCASE

# helper functions


def get_req_retry_counts(resp: requests.Response) -> int:
    """Get the retry counts for connection from requests.Response inst."""
    raw_resp: HTTPResponse = resp.raw
    if raw_resp.retries:
        return len(raw_resp.retries.history)
    return 0


# errors definition


class DownloadError(Exception):
    def __init__(self, url: str, dst: Any, *args: object) -> None:
        self.url = url
        self.dst = str(dst)
        super().__init__(*args)

    def __str__(self) -> str:
        _inject = f"failed on download url={self.url} to dst={self.dst}: "
        return f"{_inject}{super().__str__()}"

    __repr__ = __str__


class UnhandledHTTPError(DownloadError):
    """HTTPErrors that cannot be handled by us.

    Currently include 403 and 404.
    """


class DestinationNotAvailableError(DownloadError):
    pass


class ExceedMaxRetryError(DownloadError):
    pass


class ChunkStreamingError(DownloadError):
    """Exceptions that happens during chunk transfering."""


class HashVerificaitonError(DownloadError):
    pass


class DownloadFailedSpaceNotEnough(DownloadError):
    pass


class UnhandledRequestException(DownloadError):
    """requests exception that we didn't cover.
    If we get this exc, we should consider handle it.
    """


T, P = TypeVar("T"), ParamSpec("P")


def _transfer_invalid_retrier(retries: int, backoff_factor: float, backoff_max: int):
    """Retry mechanism that covers interruption/validation failed of data transfering.

    Retry mechanism applied on requests only retries during making connection to remote server,
        this retry method retries on the transfer interruption and/or recevied data invalid.
    When doing retry, this retrier will inject OTAFileCacheControl header into the request,
        to indicate the otaproxy to redo the cache for this corrupted file.

    NOTE: this retry decorator expects the input func to have 'headers' kwarg.
    NOTE: retry on errors during/after data transfering, which are ChunkStreamingError
        and HashVerificationError.
    NOTE: also cover the unhandled requests errors.
    """

    def _decorator(func: Callable[P, T]) -> Callable[P, T]:
        @wraps(func)
        def _wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            _retry_count = 0
            while True:
                try:
                    return func(*args, **kwargs)
                except (
                    HashVerificaitonError,
                    ChunkStreamingError,
                    UnhandledRequestException,
                ):
                    _retry_count += 1
                    _backoff = min(
                        backoff_max,
                        backoff_factor * (2 ** (_retry_count - 1)),
                    )

                    # inject a OTA-File-Cache-Control header to indicate ota_proxy
                    # to re-cache the possible corrupted file.
                    # modify header if needed and inject it into kwargs
                    _parsed_header: Dict[str, str] = {}

                    _popped_headers = kwargs.pop("headers", {})
                    if isinstance(_popped_headers, Mapping):
                        _parsed_header.update(_popped_headers)

                    # preserve the already set policies, while add retry_caching policy
                    _cache_policy = _parsed_header.pop(CACHE_CONTROL_HEADER, "")
                    _cache_policy = OTAFileCacheControl.update_header_str(
                        _cache_policy, retry_caching=True
                    )
                    _parsed_header[CACHE_CONTROL_HEADER] = _cache_policy

                    # replace with updated header
                    kwargs["headers"] = _parsed_header

                    if _retry_count > retries:
                        raise
                    time.sleep(_backoff)

        return _wrapper

    return _decorator


# decompression support


class DecompressionAdapterProtocol(Protocol):
    """DecompressionAdapter protocol for Downloader."""

    @abstractmethod
    def iter_chunk(self, src_stream: Union[IO[bytes], ByteString]) -> Iterator[bytes]:
        """Decompresses the source stream.

        This Method take a src_stream of compressed file and
        return another stream that yields decompressed data chunks.
        """


class ZstdDecompressionAdapter(DecompressionAdapterProtocol):
    """Zstd decompression support for Downloader."""

    def __init__(self) -> None:
        self._dctx = zstandard.ZstdDecompressor()

    def iter_chunk(self, src_stream: Union[IO[bytes], ByteString]) -> Iterator[bytes]:
        yield from self._dctx.read_to_iter(src_stream)


# downloader implementation


class Downloader:
    MAX_DOWNLOAD_THREADS = cfg.MAX_DOWNLOAD_THREAD
    MAX_CONCURRENT_DOWNLOAD = cfg.DOWNLOADER_CONNPOOL_SIZE_PER_THREAD
    CHUNK_SIZE = cfg.CHUNK_SIZE
    RETRY_COUNT = cfg.DOWNLOAD_RETRY
    BACKOFF_FACTOR = cfg.DOWNLOAD_BACKOFF_FACTOR
    OUTER_BACKOFF_FACTOR = cfg.DOWNLOAD_BACKOFF_FACTOR
    BACKOFF_MAX = cfg.DOWNLOAD_BACKOFF_MAX
    # retry on common serverside errors and clientside errors
    RETRY_ON_STATUS_CODE = {413, 429, 500, 502, 503, 504}

    DOWNLOAD_STAT_COLLECT_INTERVAL = 1
    MAX_TRAFFIC_STATS_COLLECT_PER_ROUND = 512

    def __init__(self) -> None:
        self._local = threading.local()
        self._executor = ThreadPoolExecutor(
            max_workers=min(self.MAX_DOWNLOAD_THREADS, (os.cpu_count() or 1) + 4),
            thread_name_prefix="downloader",
            initializer=self._thread_initializer,
        )
        self._hash_func = sha256
        self._proxies: Dict[str, str] = {}
        self._cookies: Dict[str, str] = {}
        self.use_http_if_http_proxy_set = False
        self.shutdowned = threading.Event()

        # downloading stats collecting
        self._workers_downloaded_bytes: Dict[int, int] = {}
        self._last_active_timestamp = 0
        self._downloaded_bytes = 0
        self._downloader_active_seconds = 0

        # launch traffic collector
        self._stats_collector = threading.Thread(target=self._download_stats_collector)
        self._stats_collector.start()

    def _thread_initializer(self):
        thread_id = threading.get_ident()
        self._workers_downloaded_bytes[thread_id] = 0

        # ------ setup the requests.Session ------ #
        session = requests.Session()
        # init retry mechanism
        # NOTE: for urllib3 version below 2.0, we have to change Retry class'
        # DEFAULT_BACKOFF_MAX, to configure the backoff max, set the value to
        # the instance will not work as increment() method will create a new
        # instance of Retry on every try without inherit the change to
        # instance's DEFAULT_BACKOFF_MAX
        Retry.DEFAULT_BACKOFF_MAX = self.BACKOFF_MAX
        retry_strategy = Retry(
            total=self.RETRY_COUNT,
            backoff_factor=self.BACKOFF_FACTOR,
            status_forcelist=self.RETRY_ON_STATUS_CODE,
            allowed_methods=["GET"],
        )
        # NOTE(20221019): download concurrent limitation is set here
        adapter = HTTPAdapter(
            pool_connections=self.MAX_CONCURRENT_DOWNLOAD,
            pool_maxsize=self.MAX_CONCURRENT_DOWNLOAD,
            max_retries=retry_strategy,
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        self._local.session = session

        # ------ compression support ------ #
        self._local._compression_support_matrix = {}
        # zstd decompression adapter
        self._local._zstd = ZstdDecompressionAdapter()
        self._local._compression_support_matrix["zst"] = self._local._zstd
        self._local._compression_support_matrix["zstd"] = self._local._zstd

    @property
    def _session(self) -> requests.Session:
        """A thread-local private session."""
        return self._local.session

    def _get_decompressor(
        self, compression_alg: Any
    ) -> Optional[DecompressionAdapterProtocol]:
        """Get thread-local private decompressor adapter accordingly."""
        return self._local._compression_support_matrix.get(compression_alg)

    @property
    def downloaded_bytes(self) -> int:
        return self._downloaded_bytes

    @property
    def downloader_active_seconds(self) -> int:
        """The accumulated time in seconds that downloader is active."""
        return self._downloader_active_seconds

    @property
    def last_active_timestamp(self) -> int:
        return self._last_active_timestamp

    def _download_stats_collector(self):
        _last_recorded_downloaded_bytes = 0
        while not self.shutdowned.is_set():
            _downloaded_bytes = sum(self._workers_downloaded_bytes.values())
            # if recorded downloaded bytes increased in this polling,
            # it means the downloader is active during the last polling period
            if _downloaded_bytes > _last_recorded_downloaded_bytes:
                _last_recorded_downloaded_bytes = _downloaded_bytes
                self._last_active_timestamp = int(time.time())
                self._downloaded_bytes = _downloaded_bytes
                self._downloader_active_seconds += self.DOWNLOAD_STAT_COLLECT_INTERVAL
            time.sleep(self.DOWNLOAD_STAT_COLLECT_INTERVAL)

    def configure_proxies(
        self, _proxies: Dict[str, str], /, use_http_if_http_proxy_set: bool = True
    ):
        self._proxies = _proxies.copy()
        self.use_http_if_http_proxy_set = use_http_if_http_proxy_set

    def configure_cookies(self, _cookies: Dict[str, str], /):
        self._cookies = _cookies.copy()

    def shutdown(self):
        """NOTE: the downloader instance cannot be reused after shutdown."""
        if not self.shutdowned.is_set():
            self.shutdowned.set()
            self._executor.shutdown()
            # wait for collector
            self._stats_collector.join()

    @staticmethod
    @contextmanager
    def _downloading_exception_mapping(url, dst):
        """Mapping requests exceptions to downloader exceptions."""
        try:
            yield
        # exception group 1: raised by requests retry
        #   RetryError: last error before exceeding limit is retrying on
        #       handled HTTP status(413, 429, 500, 502, 503, 504).
        #   ConnectionError: last error before exceeding limit is retrying
        #       on unreachable server, failed to connect to remote server.
        except (
            requests.exceptions.RetryError,
            requests.exceptions.ConnectionError,
        ) as e:
            raise ExceedMaxRetryError(
                url, dst, f"failed due to exceed max retries: {e!r}"
            )
        # exception group 2: raise during chunk transfering
        except requests.exceptions.ChunkedEncodingError as e:
            raise ChunkStreamingError(url, dst) from e
        # exception group 3: raise on unhandled HTTP error(403, 404, etc.)
        except requests.exceptions.HTTPError as e:
            _msg = f"failed to download due to unhandled HTTP error: {e.strerror}"
            logger.error(_msg)
            raise UnhandledHTTPError(url, dst, _msg) from e
        # exception group 4: any requests error escaped from the above catching
        except requests.exceptions.RequestException as e:
            _msg = f"failed due to unhandled request error: {e!r}"
            logger.error(_msg)
            raise UnhandledRequestException(url, dst, _msg) from e
        # exception group 5: file saving location not available
        except FileNotFoundError as e:
            _msg = f"failed due to dst not available: {e!r}"
            logger.error(_msg)
            raise DestinationNotAvailableError(url, dst, _msg) from e
        # exception group 6: Disk out-of-space
        except OSError as e:
            if e.errno == errno.ENOSPC:
                _msg = f"failed due to disk out-of-space: {e!r}"
                logger.error(_msg)
                raise DownloadFailedSpaceNotEnough(url, dst, _msg) from e

    @staticmethod
    def _prepare_header(
        input_header: Optional[Mapping[str, str]] = None,
        digest: Optional[str] = None,
        compression_alg: Optional[str] = None,
        proxies: Optional[Mapping[str, str]] = None,
    ) -> Optional[Mapping[str, str]]:
        """Inject ota-file-cache-control header if digest is available.

        Currently this method preserves the input_header, while
            updating/injecting Ota-File-Cache-Control header.
        """
        # NOTE: only inject ota-file-cache-control-header if we have upper otaproxy,
        #       or we have information to inject
        if not digest or not proxies:
            return input_header

        res = CIDict()
        if isinstance(input_header, Mapping):
            res.update(input_header)

        # inject digest and compression_alg into ota-file-cache-control-header
        _cache_policy = res.pop(CACHE_CONTROL_HEADER, "")
        _cache_policy = OTAFileCacheControl.update_header_str(
            _cache_policy, file_sha256=digest, file_compression_alg=compression_alg
        )
        res[CACHE_CONTROL_HEADER] = _cache_policy
        return res

    def _check_against_cache_policy_in_resp(
        self,
        url: str,
        dst: PathLike,
        digest: Optional[str],
        compression_alg: Optional[str],
        resp_headers: CIDict,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Checking digest and compression_alg against cache_policy from resp headers.

        If upper responds with file_sha256 and file_compression_alg by ota-file-cache-control header,
            use these information, otherwise use the information provided by client.

        Returns:
            A tuple of file_sha256 and file_compression_alg for the requested resources.
        """
        if not (cache_policy_str := resp_headers.get(CACHE_CONTROL_HEADER)):
            return digest, compression_alg

        cache_policy = OTAFileCacheControl.parse_header(cache_policy_str)
        if cache_policy.file_sha256:
            if digest and digest != cache_policy.file_sha256:
                _msg = (
                    f"digest({cache_policy.file_sha256}) in cache_policy"
                    f"doesn't match value({digest}) from regulars.txt: {url=}"
                )
                logger.warning(_msg)
                raise HashVerificaitonError(url, dst, _msg)

            # compression_alg from regulars.txt is set, but resp_headers indicates different
            # compression_alg.
            if compression_alg and compression_alg != cache_policy.file_compression_alg:
                logger.info(
                    f"upper serves different cache file for this OTA file: {url=}, "
                    f"use {cache_policy.file_compression_alg=} instead of {compression_alg=}"
                )
            return cache_policy.file_sha256, cache_policy.file_compression_alg
        return digest, compression_alg

    def _prepare_url(self, url: str, proxies: Optional[Dict[str, str]] = None) -> str:
        """Force changing URL scheme to HTTP if required.

        When upper otaproxy is set and use_http_if_http_proxy_set is True,
            Force changing the URL scheme to HTTP.
        """
        if self.use_http_if_http_proxy_set and proxies and "http" in proxies:
            return urlsplit(url)._replace(scheme="http").geturl()
        return url

    @_transfer_invalid_retrier(RETRY_COUNT, OUTER_BACKOFF_FACTOR, BACKOFF_MAX)
    def _download_task(
        self,
        url: str,
        dst: PathLike,
        *,
        size: Optional[int] = None,
        digest: Optional[str] = None,
        proxies: Optional[Dict[str, str]] = None,
        cookies: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
        compression_alg: Optional[str] = None,
    ) -> Tuple[int, int, int]:
        _thread_id = threading.get_ident()

        proxies = proxies or self._proxies
        prepared_url = self._prepare_url(url, proxies)

        cookies = cookies or self._cookies
        # NOTE: process headers AFTER proxies setting is parsed
        prepared_headers = self._prepare_header(
            headers, digest=digest, compression_alg=compression_alg, proxies=proxies
        )

        _hash_inst = self._hash_func()
        # NOTE: downloaded_file_size is the number of bytes we return to the caller(if compressed,
        #       the number will be of the decompressed file)
        downloaded_file_size = 0
        # NOTE: traffic_on_wire is the number of bytes we directly downloaded from remote
        traffic_on_wire = 0
        err_count = 0

        with self._downloading_exception_mapping(url, dst), self._session.get(
            prepared_url,
            stream=True,
            proxies=proxies,
            cookies=cookies,
            headers=prepared_headers,
        ) as resp, open(dst, "wb") as _dst:
            resp.raise_for_status()
            err_count = get_req_retry_counts(resp)

            digest, compression_alg = self._check_against_cache_policy_in_resp(
                url, dst, digest, compression_alg, resp.headers
            )

            # support for compresed file
            if decompressor := self._get_decompressor(compression_alg):
                for _chunk in decompressor.iter_chunk(resp.raw):
                    _hash_inst.update(_chunk)
                    _dst.write(_chunk)
                    downloaded_file_size += len(_chunk)

                    _traffic_on_wire = resp.raw.tell()
                    self._workers_downloaded_bytes[_thread_id] += (
                        _traffic_on_wire - traffic_on_wire
                    )

                    traffic_on_wire = _traffic_on_wire
            # un-compressed file
            else:
                for _chunk in resp.iter_content(chunk_size=self.CHUNK_SIZE):
                    _hash_inst.update(_chunk)
                    _dst.write(_chunk)

                    chunk_len = len(_chunk)
                    downloaded_file_size += chunk_len
                    traffic_on_wire += chunk_len
                    self._workers_downloaded_bytes[_thread_id] += chunk_len

        # checking the download result
        if size and size != downloaded_file_size:
            _msg = f"partial download detected: {size=},{downloaded_file_size=}"
            logger.warning(_msg)
            raise ChunkStreamingError(url, dst, _msg)

        if digest and ((calc_digest := _hash_inst.hexdigest()) != digest):
            _msg = (
                "sha256hash check failed detected: "
                f"act={calc_digest}, exp={digest}, {url=}"
            )
            logger.warning(_msg)
            raise HashVerificaitonError(url, dst, _msg)

        return err_count, traffic_on_wire, 0

    @copy_callable_typehint(_download_task)
    def download(self, *args, **kwargs) -> Tuple[int, int, int]:
        """A wrapper that dispatch download task to threadpool."""
        if self.shutdowned.is_set():
            raise ValueError("downloader already shutdowned.")
        return self._executor.submit(self._download_task, *args, **kwargs).result()

    @copy_callable_typehint(_download_task)
    def download_retry_inf(
        self,
        url,
        *args,
        # NOTE: inactive_timeout is hidden from the caller
        inactive_timeout: int = cfg.DOWNLOAD_GROUP_INACTIVE_TIMEOUT,
        **kwargs,
    ) -> Tuple[int, int, int]:
        """A wrapper that dispatch download task to threadpool, and keep retrying until
        downloading succeeds or exceeded inactive_timeout."""
        retry_count = 0
        while not self.shutdowned.is_set():
            try:
                return self._executor.submit(
                    self._download_task, url, *args, **kwargs
                ).result()
            except (ExceedMaxRetryError, ChunkStreamingError):
                cur_time = int(time.time())
                if (
                    self._last_active_timestamp > 0
                    and cur_time > self._last_active_timestamp + inactive_timeout
                ):
                    raise

                logger.warning(
                    f"download {url=} failed for {retry_count+1} times, still keep retrying..."
                )
                wait_with_backoff(
                    retry_count,
                    _backoff_factor=self.BACKOFF_FACTOR,
                    _backoff_max=self.BACKOFF_MAX,
                )
                retry_count += 1
                continue  # retry on recoverable downloading error
        raise ValueError(f"abort downloading {url=} as downloader shutdowned")
