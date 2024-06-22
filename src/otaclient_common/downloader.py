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

This downloader implements the OTA-Cache-File-Control protocol.

NOTE: This downloader cannot be used multi-threaded.
"""


from __future__ import annotations

import hashlib
import logging
import threading
from abc import abstractmethod
from functools import wraps
from io import IOBase
from typing import IO, Any, ByteString, Callable, Iterator, Mapping, Protocol
from urllib.parse import urlsplit

import requests
import zstandard
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict as CIDict
from urllib3.response import HTTPResponse
from urllib3.util.retry import Retry

from ota_proxy import OTAFileCacheControl
from otaclient_common.typing import P, StrOrPath, T

logger = logging.getLogger(__name__)

EMPTY_FILE_SHA256 = r"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
CACHE_CONTROL_HEADER = OTAFileCacheControl.HEADER_LOWERCASE
DEFAULT_CHUNK_SIZE = 1024**2  # 1MiB

# ------ errors definition ------ #


class DownloadError(Exception):
    """Basic Download error.

    These errors are exception that should be handled by the caller.
    Other errors raised by requests can simply be handled by outer retry.
    """


class DownloaderShutdownedError(DownloadError):
    """Raised when try to use closed downloader."""


class PartialDownloaded(DownloadError):
    """Download is not completed."""


class HashVerificaitonError(DownloadError):
    """Hash verification failed for the downloaded file."""


# ------ decompression support ------ #


class DecompressionAdapterProtocol(Protocol):
    """DecompressionAdapter protocol for Downloader."""

    @abstractmethod
    def iter_chunk(
        self, src_stream: IO[bytes] | IOBase | ByteString
    ) -> Iterator[bytes]:
        """Decompresses the source stream.

        This method takes a src_stream of compressed file and
        return another stream that yields decompressed data chunks.
        """


class ZstdDecompressionAdapter(DecompressionAdapterProtocol):
    """Zstd decompression support for Downloader."""

    def __init__(self) -> None:
        self._dctx = zstandard.ZstdDecompressor()

    def iter_chunk(self, src_stream: IO[bytes] | ByteString) -> Iterator[bytes]:
        yield from self._dctx.read_to_iter(src_stream)


# ------ OTA-Cache-File-Control protocol implementation ------ #


def inject_cache_retry_directory(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Inject a OTA-File-Cache-Control header to kwargs on hash verification failed."""
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
    """Inject ota-file-cache-control header if digest is available.

    Currently this method preserves the input_header, while
        updating/injecting Ota-File-Cache-Control header.
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

    If upper responds with file_sha256 and file_compression_alg by ota-file-cache-control header,
        use these information, otherwise use the information provided by client.

    Returns:
        A tuple of file_sha256 and file_compression_alg for the requested resources.
    """
    if not (cache_policy_str := resp_headers.get(CACHE_CONTROL_HEADER)):
        return digest, compression_alg

    cache_policy = OTAFileCacheControl.parse_header(cache_policy_str)
    if not cache_policy.file_sha256 or not cache_policy.file_compression_alg:
        return digest, compression_alg

    if digest and digest != cache_policy.file_sha256:
        _msg = (
            f"digest({cache_policy.file_sha256}) in cache_policy"
            f"doesn't match value({digest}) from regulars.txt: {url=}"
        )
        logger.warning(_msg)
        raise HashVerificaitonError(_msg)

    # compression_alg from image meta is set, but resp_headers indicates different
    # compression_alg.
    if compression_alg and compression_alg != cache_policy.file_compression_alg:
        logger.info(
            f"upper serves different cache file for this OTA file: {url=}, "
            f"use {cache_policy.file_compression_alg=} instead of {compression_alg=}"
        )
    return cache_policy.file_sha256, cache_policy.file_compression_alg


def cache_retry_decorator(func: Callable[P, T]) -> Callable[P, T]:
    """A decorator to the download API that translate internal exceptions to
    downloader exception.

    It also implements the OTA-Cache-File-Control protocol on hash verification error.
    """

    @wraps(func)
    def _wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return func(*args, **kwargs)
        except HashVerificaitonError:
            # try ONCE with headers included OTA cache control retry_caching directory,
            # if still failed, let the outer retrier does its job.
            return func(*args, **inject_cache_retry_directory(kwargs))

    return _wrapper


DEFAULT_CONNECTION_POOL_SIZE = 20
DEFAULT_RETRY_COUNT = 7
# retry on common serverside errors and clientside errors
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
        """Init an downloader instance for single thread use.

        Args:
            hash_func (Callable[..., hashlib._Hash]): The hash algorithm used to verify downloaded files.
            chunk_size (int, optional): Chunk size of chunk streaming and file writing. Defaults to 1*1024*1024.
            cookies (dict[str, str] | None, optional): Session global cookies. Defaults to None.
            proxies (dict[str, str] | None, optional): Session global proxies. Defaults to None.
        """
        self.chunk_size = chunk_size
        self.hash_func = hash_func

        self._cookies: dict[str, str] = cookies.copy() if cookies else {}
        self._proxies: dict[str, str] = proxies.copy() if proxies else {}
        self.use_http_if_http_proxy_set = use_http_if_http_proxy_set

        # downloading stats collecting
        self._downloaded_bytes = 0
        # ------ setup the requests.Session ------ #
        self._session = session = requests.Session()
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
        self._compression_support_matrix = {}
        # zstd decompression adapter
        zstd_decompressor = ZstdDecompressionAdapter()
        self._compression_support_matrix["zst"] = zstd_decompressor
        self._compression_support_matrix["zstd"] = zstd_decompressor

    def _get_decompressor(
        self, compression_alg: Any
    ) -> DecompressionAdapterProtocol | None:
        return self._compression_support_matrix.get(compression_alg)

    # API

    @property
    def downloaded_bytes(self) -> int:
        return self._downloaded_bytes

    def close(self) -> None:
        """Close the underlaying session object.

        It is OK to call this method multiple times.
        """
        self._session.close()

    @cache_retry_decorator
    def download(
        self,
        url: str,
        dst: StrOrPath,
        *,
        size: int | None = None,
        digest: str | None = None,
        headers: dict[str, str] | None = None,
        compression_alg: str | None = None,
    ) -> tuple[int, int, int]:
        """_summary_

        Args:
            url (str): _description_
            dst (StrOrPath): _description_
            size (int | None, optional): _description_. Defaults to None.
            digest (str | None, optional): _description_. Defaults to None.
            headers (dict[str, str] | None, optional): _description_. Defaults to None.
            compression_alg (str | None, optional): _description_. Defaults to None.

        Raises:
            PartialDownloaded: _description_
            HashVerificaitonError: _description_

        Returns:
            Download error, downloaded file size, traffic on wire.
        """
        proxies = self._proxies

        prepared_url = url
        if self.use_http_if_http_proxy_set and proxies and "http" in proxies:
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

        digestobj = self.hash_func()
        err_count, downloaded_file_size, traffic_on_wire = 0, 0, 0

        with self._session.get(
            prepared_url,
            stream=True,
            headers=prepared_headers,
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
                err_count = len(_retries)

            if decompressor := self._get_decompressor(compression_alg):
                data_iter = decompressor.iter_chunk(raw_resp)
            else:
                data_iter = resp.iter_content(chunk_size=self.chunk_size)

            for _chunk in data_iter:
                digestobj.update(_chunk)
                dst_fp.write(_chunk)
                downloaded_file_size += len(_chunk)

                new_traffic_on_wire = raw_resp.tell()
                if (increased_traffic := new_traffic_on_wire - traffic_on_wire) > 0:
                    self._downloaded_bytes += increased_traffic
                traffic_on_wire = new_traffic_on_wire

        if size and size != downloaded_file_size:
            _err_msg = (
                f"detect partial downloading: {size=} != {downloaded_file_size=} for "
                f"{prepared_url}, saving to {dst}"
            )
            raise PartialDownloaded(_err_msg)

        if digest and ((calc_digest := digestobj.hexdigest()) != digest):
            _err_msg = f"hash verification failed: {digest=} != {calc_digest=} for {prepared_url}"
            raise HashVerificaitonError(_err_msg)

        return err_count, downloaded_file_size, traffic_on_wire


class DownloaderPool:
    """A pool of downloader instances for multi-threading.

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
        if _res := sum(downloader.downloaded_bytes for downloader in self._instances):
            self._total_downloaded_bytes = _res
        return self._total_downloaded_bytes

    def get_instance(self) -> Downloader:
        """Get a weakref to the thread-specific downloader instance.

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
        """Close the downloader instances."""
        for _instance in self._instances:
            _instance.close()
        self._instances = []
