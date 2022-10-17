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

from abc import abstractmethod
import errno
import os
import requests
import threading
import time
import zstandard
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from hashlib import sha256
from pathlib import Path
from typing import (
    IO,
    Any,
    ByteString,
    Callable,
    Dict,
    Optional,
    Protocol,
    Union,
)
from requests.adapters import HTTPAdapter
from requests.exceptions import (
    HTTPError,
    ChunkedEncodingError,
    ConnectionError,
    RequestException,
    RetryError,
)
from urllib3.util.retry import Retry

from .configs import config as cfg
from .common import OTAFileCacheControl
from . import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


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

    pass


class DestinationNotAvailableError(DownloadError):
    pass


class ExceedMaxRetryError(DownloadError):
    pass


class ChunkStreamingError(DownloadError):
    """Exceptions that happens during chunk transfering."""

    pass


class HashVerificaitonError(DownloadError):
    pass


class DownloadFailedSpaceNotEnough(DownloadError):
    pass


REQUEST_RECACHE_HEADER: Dict[str, str] = {
    OTAFileCacheControl.header_lower.value: OTAFileCacheControl.retry_caching.value
}


def _retry(retry: int, backoff_factor: float, backoff_max: int, func: Callable):
    """
    NOTE: this retry decorator expects the input func to have 'headers' kwarg.
    NOTE: only retry on errors that doesn't handled by the urllib3.Retry module,
          which are ChunkStreamingError and HashVerificationError.
    """
    from functools import wraps

    @wraps(func)
    def _wrapper(*args, **kwargs):
        _retry_count = 0
        while True:
            try:
                return func(*args, **kwargs)
            except (ExceedMaxRetryError, UnhandledHTTPError):
                # if urllib3.Retry has already handled the retry,
                # or we hit an UnhandledHTTPError, just re-raise
                raise
            except (HashVerificaitonError, ChunkStreamingError):
                _retry_count += 1
                _backoff = min(backoff_max, backoff_factor * (2 ** (_retry_count - 1)))

                # inject a OTA-File-Cache-Control header to indicate ota_proxy
                # to re-cache the possible corrupted file.
                # modify header if needed and inject it into kwargs
                if "headers" in kwargs and isinstance(kwargs["headers"], dict):
                    kwargs["headers"].update(REQUEST_RECACHE_HEADER.copy())
                else:
                    kwargs["headers"] = REQUEST_RECACHE_HEADER.copy()

                if _retry_count > retry:
                    raise
                time.sleep(_backoff)

    return _wrapper


class DecompressionAdapterProtocol(Protocol):
    """DecompressionAdapter protocol for Downloader."""

    @abstractmethod
    def stream_decompressor(
        self, src_stream: Union[IO[bytes], ByteString]
    ) -> IO[bytes]:
        """Decompresses the source stream.

        This Method take a src_stream of compressed file and
        return another stream that yields decompressed data chunks.
        """


class ZstdDecompressionAdapter(DecompressionAdapterProtocol):
    """Zstd decompression support for Downloader."""

    def __init__(self) -> None:
        self._dctx = zstandard.ZstdDecompressor()

    def stream_decompressor(
        self, src_stream: Union[IO[bytes], ByteString]
    ) -> IO[bytes]:
        return self._dctx.stream_reader(src_stream)


class Downloader:
    EMPTY_STR_SHA256 = (
        r"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    MAX_DOWNLOAD_THREADS = cfg.MAX_DOWNLOAD_THREAD
    MAX_CONCURRENT_DOWNLOAD = cfg.DOWNLOADER_CONNPOOL_SIZE_PER_THREAD
    CHUNK_SIZE = cfg.CHUNK_SIZE
    RETRY_COUNT = cfg.DOWNLOAD_RETRY
    BACKOFF_FACTOR = 1
    OUTER_BACKOFF_FACTOR = 0.01
    BACKOFF_MAX = cfg.DOWNLOAD_BACKOFF_MAX
    # retry on common serverside errors and clientside errors
    RETRY_ON_STATUS_CODE = {413, 429, 500, 502, 503, 504}

    def _thread_initializer(self):
        ### setup the requests.Session ###
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
        adapter = HTTPAdapter(
            pool_connections=self.MAX_CONCURRENT_DOWNLOAD,
            pool_maxsize=self.MAX_CONCURRENT_DOWNLOAD,
            max_retries=retry_strategy,
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        self._local.session = session

        ### compression support ###
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
        self, compression_alg: str
    ) -> Optional[DecompressionAdapterProtocol]:
        """Get thread-local private decompressor adapter accordingly."""
        return self._local._compression_support_matrix.get(compression_alg)

    def __init__(self) -> None:
        self._local = threading.local()
        self._executor = ThreadPoolExecutor(
            max_workers=min(self.MAX_DOWNLOAD_THREADS, (os.cpu_count() or 1) + 4),
            thread_name_prefix="downloader",
            initializer=self._thread_initializer,
        )
        self._hash_func = sha256

    def shutdown(self):
        self._executor.shutdown()

    @partial(_retry, RETRY_COUNT, OUTER_BACKOFF_FACTOR, BACKOFF_MAX)
    def _download_task(
        self,
        url: str,
        dst: Union[str, Path],
        *,
        size: Optional[int] = None,
        digest: Optional[str] = None,
        proxies: Optional[Dict[str, str]] = None,
        cookies: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
        compression_alg: Optional[str] = None,
    ) -> int:
        # special treatment for empty file
        if dst == self.EMPTY_STR_SHA256 and not (dst_p := Path(dst)).is_file():
            dst_p.write_bytes(b"")
            return 0

        _hash_inst, _downloaded_bytes = self._hash_func(), 0
        _err_count = 0
        try:
            with self._session.get(
                url,
                stream=True,
                proxies=proxies,
                cookies=cookies,
                headers=headers,
            ) as resp, open(dst, "wb") as f:
                resp.raise_for_status()
                raw_resp = resp.raw
                if raw_resp.retries:
                    _err_count = len(raw_resp.retries.history)
                # support for compressed file
                if compression_alg and (
                    decompressor := self._get_decompressor(compression_alg)
                ):
                    raw_resp = decompressor.stream_decompressor(raw_resp)
                while data := raw_resp.read(self.CHUNK_SIZE):
                    _hash_inst.update(data)
                    f.write(data)
                    _downloaded_bytes += len(data)
        except RetryError as e:
            raise ExceedMaxRetryError(url, dst, f"{e!r}")
        except (ChunkedEncodingError, ConnectionError) as e:
            # streaming interrupted
            raise ChunkStreamingError(url, dst) from e
        except HTTPError as e:
            # HTTPErrors that cannot be handled by retry,
            # include 403 and 404
            raise UnhandledHTTPError(url, dst, e.strerror)
        except RequestException as e:
            # any errors that happens during handling request
            # and are not the above exceptions
            raise ExceedMaxRetryError(url, dst) from e
        except FileNotFoundError as e:
            # dst is not available
            raise DestinationNotAvailableError(url, dst) from e
        except OSError as e:
            # only handle disk out-of-space error
            if e.errno == errno.ENOSPC:
                raise DownloadFailedSpaceNotEnough(url, dst) from None

        # checking the download result
        if size is not None and size != _downloaded_bytes:
            msg = f"partial download detected: {size=},{_downloaded_bytes=}"
            logger.error(msg)
            raise ChunkStreamingError(url, dst, msg)
        if digest and ((calc_digest := _hash_inst.hexdigest()) != digest):
            msg = (
                "sha256hash check failed detected: "
                f"act={calc_digest}, exp={digest}, {url=}"
            )
            logger.error(msg)
            raise HashVerificaitonError(url, dst, msg)

        return _err_count

    def download(
        self,
        url: str,
        dst: Union[str, Path],
        *,
        size: Optional[int] = None,
        digest: Optional[str] = None,
        proxies: Optional[Dict[str, str]] = None,
        cookies: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
        compression_alg: Optional[str] = None,
    ):
        """Dispatcher for download tasks."""
        return self._executor.submit(
            self._download_task,
            url,
            dst,
            size=size,
            digest=digest,
            proxies=proxies,
            cookies=cookies,
            headers=headers,
            compression_alg=compression_alg,
        ).result()
