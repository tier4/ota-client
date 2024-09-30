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
"""A common used downloader implementation for otaclient.

This downloader implements the OTA-Cache-File-Control protocol to co-operate with otaproxy.
"""


from __future__ import annotations

import hashlib
import logging
import threading
import time
from abc import abstractmethod
from functools import wraps
from typing import IO, Any, ByteString, Callable, Iterator, Mapping, Protocol, TypedDict
from urllib.parse import urlsplit

import requests
import zstandard
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict as CIDict
from requests.utils import add_dict_to_cookiejar
from urllib3.response import HTTPResponse
from urllib3.util.retry import Retry

from ota_proxy import OTAFileCacheControl
from otaclient_common.typing import P, StrOrPath, T

logger = logging.getLogger(__name__)

EMPTY_FILE_SHA256 = r"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
CACHE_CONTROL_HEADER = OTAFileCacheControl.HEADER_LOWERCASE
DEFAULT_CHUNK_SIZE = 1024**2  # 1MiB
DEFAULT_CONNECTION_TIMEOUT = 16  # seconds
DEFAULT_READ_TIMEOUT = 32  # seconds

# ------ errors definition ------ #


class DownloadError(Exception):
    """Base Download error.

    These errors are the one that directly raised by Downloader itself.
    For other underlying exceptions like exceptions from requests, Downloader
        will not wrap or capture these exceptions but just let it passthrough
        to the upper caller.
    """


class PartialDownload(DownloadError):
    """Download is not completed.

    When the expected <size> of file is known, downloader will use
        this information to check the downloaded file.
    """


class HashVerificationError(DownloadError):
    """Hash verification failed for the downloaded file."""


class BrokenDecompressionError(DownloadError):
    """Failed to decompress the downloading file stream.

    This might happen if the compression_alg information is incorrect.
    """


# ------ decompression support ------ #


class DecompressionAdapter(Protocol):
    """DecompressionAdapter protocol for Downloader."""

    @abstractmethod
    def iter_chunk(self, src_stream: IO[bytes] | ByteString) -> Iterator[bytes]:
        """Decompresses the source stream.

        This method takes a src_stream of compressed file and
        return another stream that yields decompressed data chunks.
        """


# cspell:words decompressor dctx
class ZstdDecompressionAdapter(DecompressionAdapter):
    """Zstd decompression support for Downloader."""

    def __init__(self) -> None:
        self._dctx = zstandard.ZstdDecompressor()

    def iter_chunk(self, src_stream: IO[bytes] | ByteString) -> Iterator[bytes]:
        try:
            yield from self._dctx.read_to_iter(src_stream)
        except zstandard.ZstdError as e:
            raise BrokenDecompressionError(
                f"failure during decompressing file stream: {e!r}"
            ) from e


# ------ OTA-Cache-File-Control protocol implementation ------ #


def inject_cache_retry_directory(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Inject a OTA-File-Cache-Control header to kwargs on the retry request for hash verification failed.

    When upper proxy is an otaproxy, this header will indicate the otaproxy to clear the cache
        for this file and try re-downloading and re-cache from remote.
    """
    parsed_header: dict[str, str] = {}

    input_headers = kwargs.pop("headers", None)
    if isinstance(input_headers, Mapping):
        parsed_header.update(input_headers)

    # preserve the already set policies, while add retry_caching policy
    cache_policy = parsed_header.pop(CACHE_CONTROL_HEADER, "")
    cache_policy = OTAFileCacheControl.update_header_str(
        cache_policy, retry_caching=True
    )
    parsed_header[CACHE_CONTROL_HEADER] = cache_policy

    kwargs["headers"] = parsed_header
    return kwargs


def inject_cache_control_header_in_req(
    *,
    digest: str,
    input_header: Mapping[str, str] | None = None,
    compression_alg: str | None = None,
) -> Mapping[str, str] | None:
    """Inject ota-file-cache-control header into request if extra info is available.

    This method injects the Ota-File-Cache-Control header into request when upper
        proxy is an otaproxy, to provide extra information to otaproxy, including
        digest and compression_alg for the requested file.
    """
    prepared_headers = CIDict()
    if isinstance(input_header, Mapping):
        prepared_headers.update(input_header)

    # inject digest and compression_alg into ota-file-cache-control-header
    cache_policy = prepared_headers.pop(CACHE_CONTROL_HEADER, "")
    cache_policy = OTAFileCacheControl.update_header_str(
        cache_policy,
        file_sha256=digest,
        file_compression_alg=compression_alg,
    )
    prepared_headers[CACHE_CONTROL_HEADER] = cache_policy
    return prepared_headers


def check_cache_policy_in_resp(
    url: str,
    *,
    compression_alg: str | None = None,
    digest: str | None = None,
    resp_headers: CIDict,
) -> tuple[str | None, str | None]:
    """Checking digest and compression_alg against cache_policy from resp headers.

    If both image meta and cache-control header specify the digest, check the matching,
      raise exception if unmatched as the remote resources might be corrupted.
    Note that we always use the digest from image meta.

    If ache-control header indicates different compression_alg than image meta,
        we use the value from cache-control header.

    Returns:
        A tuple of file_sha256 and file_compression_alg for the requested resources.
    """
    if not (cache_policy_str := resp_headers.get(CACHE_CONTROL_HEADER)):
        return digest, compression_alg

    cache_policy = OTAFileCacheControl.parse_header(cache_policy_str)
    # NOTE: we always use the digest from image meta if set.
    if digest and cache_policy.file_sha256 and digest != cache_policy.file_sha256:
        _msg = (
            f"digest({cache_policy.file_sha256}) in cache_policy"
            f"doesn't match value({digest}) from regulars.txt: {url=}"
        )
        logger.warning(_msg)
        raise HashVerificationError(_msg)

    # If compression_alg mismatched, use the one from cache-control header.
    # NOTE that if otaproxy's information is also wrong, it can be covered by
    #   the DecompressionAdaptor, and trigger a retry-cache.
    if (
        cache_policy.file_compression_alg
        and compression_alg != cache_policy.file_compression_alg
    ):
        logger.warning(
            f"upper indicates different compression_alg for this OTA file: {url=}, "
            f"use {cache_policy.file_compression_alg=} instead of {compression_alg=}"
        )
        compression_alg = cache_policy.file_compression_alg
    return digest, compression_alg


def retry_on_digest_mismatch(func: Callable[P, T]) -> Callable[P, T]:
    """A decorator to the download API that translate internal exceptions to
    downloader exception.

    It also implements the OTA-Cache-File-Control protocol on hash verification error, or
        the decompression is broken(possibly due to wrong compression_alg info).
    """

    @wraps(func)
    def _wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return func(*args, **kwargs)
        except (HashVerificationError, BrokenDecompressionError) as e:
            # try ONCE with headers included OTA cache control retry_caching directory,
            # if still failed, let the outer retry mechanism does its job.
            logger.warning(f"trigger cache retry due to: {e!r}")
            return func(*args, **inject_cache_retry_directory(kwargs))

    return _wrapper


DEFAULT_CONNECTION_POOL_SIZE = 20
DEFAULT_RETRY_COUNT = 7
# retry on common server-side errors and client-side errors
DEFAULT_RETRY_STATUS = frozenset([413, 429, 500, 502, 503, 504])


class Downloader:

    def __init__(
        self,
        *,
        hash_func: Callable[..., hashlib._Hash],
        chunk_size: int,
        use_http_if_http_proxy_set: bool = True,
        cookies: dict[str, str] | None = None,
        proxies: dict[str, str] | None = None,
        connection_pool_size: int = DEFAULT_CONNECTION_POOL_SIZE,
        retry_on_status: frozenset[int] = DEFAULT_RETRY_STATUS,
        retry_count: int = DEFAULT_RETRY_COUNT,
    ) -> None:
        """Init an instance of Downloader.

        Note that Downloader instance cannot be used in multi-threading. Use DownloaderPool
            for multi-threading environment.

        Args:
            hash_func (Callable[..., hashlib._Hash]): The hash algorithm to validate downloaded files.
            chunk_size (int): Size of chunk when stream downloading the files.
            use_http_if_http_proxy_set (bool, optional): Force HTTP when HTTP proxy is set. Defaults to True.
                This must be True when upper proxy is otaproxy.
            cookies (dict[str, str] | None, optional): Downloader inst scope cookies. Defaults to None.
            proxies (dict[str, str] | None, optional): Downloader inst scope proxies setting. Defaults to None.
            connection_pool_size (int, optional): Defaults to DEFAULT_CONNECTION_POOL_SIZE.
            retry_on_status (frozenset[int], optional): A list of status code that requests internally will retry on.
                Defaults to DEFAULT_RETRY_STATUS.
            retry_count (int, optional): Max retry count for requests internal retry. Defaults to DEFAULT_RETRY_COUNT.
        """
        self.chunk_size = chunk_size
        self.hash_func = hash_func

        parsed_cookies = cookies.copy() if cookies else {}
        self._proxies = parsed_proxies = proxies.copy() if proxies else {}
        self._force_http = use_http_if_http_proxy_set and "http" in parsed_proxies

        # downloading stats collecting
        self._downloaded_bytes = 0
        # ------ setup the requests.Session ------ #
        self._session = session = requests.Session()

        # configure cookies and proxies
        # NOTE that proxies setting here will be overwritten by environmental variables
        #   if proxies are also set by environmental variables.
        session.proxies.update(parsed_proxies)
        session.cookies = add_dict_to_cookiejar(session.cookies, parsed_cookies)

        # configure retry strategy
        # cspell:words forcelist
        http_adapter = HTTPAdapter(
            pool_connections=connection_pool_size,
            pool_maxsize=connection_pool_size,
            max_retries=Retry(
                total=retry_count,
                status_forcelist=retry_on_status,
                allowed_methods=["GET"],
            ),
        )
        session.mount("https://", http_adapter)
        session.mount("http://", http_adapter)

        # ------ compression support ------ #
        self._compression_support_matrix: dict[str, DecompressionAdapter] = {}
        # zstd decompression adapter
        zstd_decompressor = ZstdDecompressionAdapter()
        self._compression_support_matrix["zst"] = zstd_decompressor
        self._compression_support_matrix["zstd"] = zstd_decompressor

    def _get_decompressor(
        self, compression_alg: str | Any
    ) -> DecompressionAdapter | None:
        """Get decompressor according to compression_alg."""
        return self._compression_support_matrix.get(compression_alg)

    # API

    @property
    def downloaded_bytes(self) -> int:
        """The total bytes on wire of this Downloader inst."""
        return self._downloaded_bytes

    def close(self) -> None:
        """Close the underlying requests.Session instance.

        It is OK to call this method multiple times.
        """
        self._session.close()

    @retry_on_digest_mismatch
    def download(
        self,
        url: str,
        dst: StrOrPath,
        *,
        size: int | None = None,
        digest: str | None = None,
        headers: dict[str, str] | None = None,
        compression_alg: str | None = None,
        timeout: tuple[int, int] | None = (
            DEFAULT_CONNECTION_TIMEOUT,
            DEFAULT_READ_TIMEOUT,
        ),
    ) -> tuple[int, int, int]:
        """Download one file with the Downloader instance.

        Args:
            url (str): The URL of file to be downloaded.
            dst (StrOrPath): The destination at local disk to save the downloaded file.
            size (int | None, optional): The expected size of the downloaded file. Defaults to None.
            digest (str | None, optional): The expected digest of the downloaded file. Defaults to None.
            headers (dict[str, str] | None, optional): Extra headers to use for request. Defaults to None.
            compression_alg (str | None, optional): The expected compression alg for the file. Defaults to None.
                NOTE: don't confuse with the HTTP compression.
            timeout (tuple[int, int] | None): A tuple of sock connection timeout and read timeout. Defaults to
                (DEFAULT_CONNECTION_TIMEOUT, DEFAULT_READ_TIMEOUT).

        Raises:
            PartialDownloaded: When <size> is specified and the downloaded file's size doesn't match the expected size.
            HashVerificationError: When <digest> is specified and the downloaded file's digest doesn't match the expected digest.

        Returns:
            A tuple of ints of download_error(total_retry_counts), downloaded_file_size and traffic_on_wire.
        """
        proxies = self._proxies

        prepared_url = url
        if self._force_http:
            prepared_url = urlsplit(url)._replace(scheme="http").geturl()

        # NOTE: only inject ota-file-cache-control-header if we have upper otaproxy,
        #       or we have information to inject
        if digest and proxies:
            # NOTE: process headers AFTER proxies setting is parsed
            prepared_headers = inject_cache_control_header_in_req(
                digest=digest,
                input_header=headers,
                compression_alg=compression_alg,
            )
        else:
            prepared_headers = headers

        # cspell:ignore digestobj
        digestobj = self.hash_func()
        err_count, downloaded_file_size, traffic_on_wire = 0, 0, 0

        with self._session.get(
            prepared_url, stream=True, headers=prepared_headers, timeout=timeout
        ) as resp, open(dst, "wb") as dst_fp:
            resp.raise_for_status()

            digest, compression_alg = check_cache_policy_in_resp(
                url,
                compression_alg=compression_alg,
                digest=digest,
                resp_headers=resp.headers,
            )

            raw_resp: HTTPResponse = resp.raw
            if _retries := raw_resp.retries:
                err_count = len(_retries.history)

            if decompressor := self._get_decompressor(compression_alg):
                # NOTE: raw_resp(HTTPResponse) here is configured to be an IO[bytes]
                for _chunk in decompressor.iter_chunk(raw_resp):  # type: ignore
                    digestobj.update(_chunk)
                    dst_fp.write(_chunk)
                    downloaded_file_size += len(_chunk)

                    new_traffic_on_wire = raw_resp.tell()
                    self._downloaded_bytes += new_traffic_on_wire - traffic_on_wire
                    traffic_on_wire = new_traffic_on_wire
            else:  # no compression is configured
                for _chunk in resp.iter_content(chunk_size=self.chunk_size):
                    digestobj.update(_chunk)
                    dst_fp.write(_chunk)
                    downloaded_file_size += len(_chunk)

                    _read_size = len(_chunk)
                    self._downloaded_bytes += _read_size
                    traffic_on_wire += _read_size

        if size and size != downloaded_file_size:
            _err_msg = (
                f"detect partial downloading: {size=} != {downloaded_file_size=} for "
                f"{prepared_url}, saving to {dst}"
            )
            raise PartialDownload(_err_msg)

        if digest and ((calc_digest := digestobj.hexdigest()) != digest):
            _err_msg = f"hash verification failed: {digest=} != {calc_digest=} for {prepared_url}"
            raise HashVerificationError(_err_msg)

        return err_count, downloaded_file_size, traffic_on_wire


class DownloadPoolWatchdogFuncContext(TypedDict):
    downloaded_bytes: int
    previous_active_timestamp: int


class DownloaderPool:
    """A pool of downloader instances for multi-threading environment.

    Each worker thread can get a thread-local instance of downloader.
    """

    INSTANCE_AVAILABLE_ID = 0

    def __init__(
        self,
        instance_num: int,
        hash_func: Callable[..., hashlib._Hash],
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        cookies: dict[str, str] | None = None,
        proxies: dict[str, str] | None = None,
    ) -> None:
        self._instances: list[Downloader] = [
            Downloader(
                hash_func=hash_func,
                chunk_size=chunk_size,
                cookies=cookies,
                proxies=proxies,
            )
            for _ in range(instance_num)
        ]

        self._instance_map_lock = threading.Lock()
        self._idx_thread_mapping: list[int] = [
            self.INSTANCE_AVAILABLE_ID for _ in range(instance_num)
        ]
        self._thread_idx_mapping: dict[int, int] = {}
        self._total_downloaded_bytes = 0

    @property
    def total_downloaded_bytes(self) -> int:
        """The sum of all downloader instances' total downloaded bytes on wire."""
        if _res := sum(downloader.downloaded_bytes for downloader in self._instances):
            self._total_downloaded_bytes = _res
        return self._total_downloaded_bytes

    def downloading_watchdog(
        self, *, ctx: DownloadPoolWatchdogFuncContext, max_idle_timeout: int
    ):
        """Downloading pool idle timeout watchdog.

        This method is designed to use with ThreadPoolExecutorWithRetry.
        When configured to use, if the downloading is idle longer than <max_idle_timeout>,
            call to this function will raise ValueError.
        """
        downloaded_bytes = self.total_downloaded_bytes

        current_tiemstamp = int(time.time())
        if downloaded_bytes > ctx["downloaded_bytes"]:
            ctx["downloaded_bytes"] = downloaded_bytes
            ctx["previous_active_timestamp"] = current_tiemstamp
            return

        if current_tiemstamp - ctx["previous_active_timestamp"] > max_idle_timeout:
            _err_msg = f"downloader stuck for {max_idle_timeout} seconds, abort"
            logger.error(_err_msg)
            raise ValueError(_err_msg)

    def get_instance(self) -> Downloader:
        """Get a reference of the downloader instance for the calling thread.

        NOTE: this method is thread-specific, and will return the same instance
            for multiple calls from the same thread.

        Raises:
            ValueError if no available idle instance for caller thread.
        """
        native_thread_id = threading.get_native_id()
        with self._instance_map_lock:
            if native_thread_id in self._thread_idx_mapping:
                idx = self._thread_idx_mapping[native_thread_id]
                return self._instances[idx]

            # the caller thread doesn't have an assigned downloader instance yet,
            #   find one available instance for it.
            for idx, _thread_id in enumerate(self._idx_thread_mapping):
                if _thread_id == self.INSTANCE_AVAILABLE_ID:
                    first_available_idx = idx
                    break
            else:
                raise ValueError("no idle downloader instance available")

            self._idx_thread_mapping[first_available_idx] = native_thread_id
            self._thread_idx_mapping[native_thread_id] = first_available_idx
            return self._instances[first_available_idx]

    def release_instance(self) -> None:
        """Release one instance back to the pool.

        This method is intended to be called at the thread that uses the instance.
        """
        native_thread_id = threading.get_native_id()
        with self._instance_map_lock:
            idx = self._thread_idx_mapping.pop(native_thread_id, None)
            if idx is not None:
                self._idx_thread_mapping[idx] = self.INSTANCE_AVAILABLE_ID

    def release_all_instances(self) -> None:
        """Clear the instances-thread mapping.

        This method should be called at the main thread.
        """
        with self._instance_map_lock:
            self._idx_thread_mapping = [self.INSTANCE_AVAILABLE_ID] * len(
                self._idx_thread_mapping
            )
            self._thread_idx_mapping = {}

    def shutdown(self) -> None:
        """Close all the downloader instances."""
        # at final, trigger an update to the total_downloaded_bytes, in case
        #   we still need the total_downloaded_bytes data after pool shutdown.
        _ = self.total_downloaded_bytes

        with self._instance_map_lock:
            for _instance in self._instances:
                _instance.close()
            self._instances = []
