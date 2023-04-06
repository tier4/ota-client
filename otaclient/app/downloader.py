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


import errno
import os
import requests
import threading
import time
import zstandard
import requests.exceptions
import urllib3.exceptions
from abc import abstractmethod
from concurrent.futures import ThreadPoolExecutor
from queue import Empty, Queue
from functools import partial
from hashlib import sha256
from pathlib import Path
from typing import (
    IO,
    Any,
    ByteString,
    Callable,
    Dict,
    Iterator,
    Optional,
    Protocol,
    Tuple,
    Union,
)
from requests.adapters import HTTPAdapter
from urllib.parse import urlsplit
from urllib3.util.retry import Retry
from urllib3.response import HTTPResponse

from .configs import config as cfg
from .common import OTAFileCacheControl
from . import log_setting

logger = log_setting.get_logger(
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


class Downloader:
    EMPTY_STR_SHA256 = (
        r"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
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
        self._proxies: Optional[Dict[str, str]] = None
        self._cookies: Optional[Dict[str, str]] = None
        self.shutdowned = threading.Event()

        # downloading stats collecting
        self._traffic_report_que = Queue()
        self._downloading_thread_active_flag: Dict[int, threading.Event] = {}
        self._last_active_timestamp = 0
        self._downloaded_bytes = 0
        self._downloader_active_seconds = 0

        # launch traffic collector
        self._stats_collector = threading.Thread(target=self._download_stats_collector)
        self._stats_collector.start()

    def _thread_initializer(self):
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

        # ------ download timing flag ------ #
        self._downloading_thread_active_flag[
            threading.get_native_id()
        ] = threading.Event()

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
        while not self.shutdowned.is_set():
            time.sleep(self.DOWNLOAD_STAT_COLLECT_INTERVAL)
            # ------ collect downloading_elapsed time by sampling ------ #
            # if any of the threads is actively downloading,
            # we update the last_active_timestamp.
            if any(
                map(
                    lambda _event: _event.is_set(),
                    self._downloading_thread_active_flag.values(),
                )
            ):
                self._last_active_timestamp = int(time.time())
                self._downloader_active_seconds += self.DOWNLOAD_STAT_COLLECT_INTERVAL

            # ------ collect downloaded bytes ------ #
            if self._traffic_report_que.empty():
                continue

            traffic_bytes = 0
            try:
                for _ in range(self.MAX_TRAFFIC_STATS_COLLECT_PER_ROUND):
                    traffic_bytes += self._traffic_report_que.get_nowait()
            except Empty:
                pass
            self._downloaded_bytes += traffic_bytes

    def configure_proxies(self, _proxies: Dict[str, str], /):
        self._proxies = _proxies.copy()

    def configure_cookies(self, _cookies: Dict[str, str], /):
        self._cookies = _cookies.copy()

    def shutdown(self):
        """NOTE: the downloader instance cannot be reused after shutdown."""
        if not self.shutdowned.is_set():
            self.shutdowned.set()
            self._executor.shutdown()
            # wait for collector
            self._stats_collector.join()

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
        use_http_if_proxy_set: bool = True,
    ) -> Tuple[int, int, int]:
        # special treatment for empty file
        if digest == self.EMPTY_STR_SHA256:
            if not (dst_p := Path(dst)).is_file():
                dst_p.write_bytes(b"")
            return 0, 0, 0

        # NOTE: if proxy is set and use_http_if_proxy_set is true,
        #       unconditionally change scheme to HTTP
        _proxies = proxies or self._proxies
        if _proxies and use_http_if_proxy_set and "http" in _proxies:
            url = urlsplit(url)._replace(scheme="http").geturl()
        # use input cookies or inst's cookie
        _cookies = cookies or self._cookies

        # NOTE: downloaded_file_size is the number of bytes we return to the caller(if compressed,
        #       the number will be of the decompressed file)
        _hash_inst, downloaded_file_size = self._hash_func(), 0
        # NOTE: traffic_on_wire is the number of bytes we directly downloaded from remote
        traffic_on_wire = 0
        _err_count = 0
        # flag this thread as actively downloading thread
        active_flag = self._downloading_thread_active_flag[threading.get_native_id()]
        try:
            with self._session.get(
                url,
                stream=True,
                proxies=_proxies,
                cookies=_cookies,
                headers=headers,
            ) as resp, open(dst, "wb") as _dst:
                resp.raise_for_status()
                # NOTE: mark this downloading thread as active after
                #       the connection is made.
                active_flag.set()

                raw_resp: HTTPResponse = resp.raw
                if raw_resp.retries:
                    _err_count = len(raw_resp.retries.history)

                # support for compresed file
                if decompressor := self._get_decompressor(compression_alg):
                    for _chunk in decompressor.iter_chunk(resp.raw):
                        _hash_inst.update(_chunk)
                        _dst.write(_chunk)
                        downloaded_file_size += len(_chunk)

                        _traffic_on_wire = raw_resp.tell()
                        self._traffic_report_que.put_nowait(
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
                        self._traffic_report_que.put_nowait(chunk_len)
                        traffic_on_wire += chunk_len
        except requests.exceptions.RetryError as e:
            raise ExceedMaxRetryError(url, dst, f"{e!r}")
        except (
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ConnectionError,
            urllib3.exceptions.ProtocolError,
        ) as e:
            # streaming interrupted
            raise ChunkStreamingError(url, dst) from e
        except requests.exceptions.HTTPError as e:
            # HTTPErrors that cannot be handled by retry,
            # include 403 and 404
            raise UnhandledHTTPError(url, dst, e.strerror)
        except (
            requests.exceptions.RequestException,
            urllib3.exceptions.HTTPError,
        ) as e:
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
        finally:
            # download is finished, clear the active flag
            active_flag.clear()

        # checking the download result
        if size is not None and size != downloaded_file_size:
            msg = f"partial download detected: {size=},{downloaded_file_size=}"
            logger.error(msg)
            raise ChunkStreamingError(url, dst, msg)
        if digest and ((calc_digest := _hash_inst.hexdigest()) != digest):
            msg = (
                "sha256hash check failed detected: "
                f"act={calc_digest}, exp={digest}, {url=}"
            )
            logger.error(msg)
            raise HashVerificaitonError(url, dst, msg)

        return _err_count, traffic_on_wire, 0

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
        use_http_if_proxy_set: bool = True,
    ) -> Tuple[int, int, int]:
        """Dispatcher for download tasks.

        Returns:
            A tuple of ints, which are error counts, real downloaded bytes
                and a const 0.
        """
        if self.shutdowned.is_set():
            raise ValueError("downloader already shutdowned.")

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
            use_http_if_proxy_set=use_http_if_proxy_set,
        ).result()
