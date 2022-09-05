import requests
import time
import errno
from functools import partial
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Union
from urllib.parse import quote_from_bytes, urljoin, urlparse
from requests.exceptions import (
    ConnectionError,
    ChunkedEncodingError,
    StreamConsumedError,
)

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
    NOTE: this retry decorator expects the input func to
    have 'headers' kwarg.
    NOTE 2: only retry on ChunkStreamingError and HashVerificationError
    """
    from functools import wraps

    @wraps(func)
    def _wrapper(*args, **kwargs):
        _retry_count = 0
        while True:
            try:
                return func(*args, **kwargs)
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


class Downloader:
    CHUNK_SIZE = cfg.CHUNK_SIZE
    RETRY_COUNT = cfg.DOWNLOAD_RETRY
    BACKOFF_FACTOR = 1
    OUTER_BACKOFF_FACTOR = 0.01
    BACKOFF_MAX = cfg.DOWNLOAD_BACKOFF_MAX
    MAX_CONCURRENT_DOWNLOAD = cfg.MAX_CONCURRENT_DOWNLOAD
    EMPTY_STR_SHA256 = (
        r"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )

    def __init__(self):
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        # base session
        session = requests.Session()

        # init retry mechanism
        # NOTE: for urllib3 version below 2.0, we have to change Retry class' DEFAULT_BACKOFF_MAX,
        # to configure the backoff max, set the value to the instance will not work as increment() method
        # will create a new instance of Retry on every try without inherit the change to instance's DEFAULT_BACKOFF_MAX
        Retry.DEFAULT_BACKOFF_MAX = self.BACKOFF_MAX
        retry_strategy = Retry(
            total=self.RETRY_COUNT,
            raise_on_status=True,
            backoff_factor=self.BACKOFF_FACTOR,
            # retry on common server side errors and non-critical client side errors
            status_forcelist={413, 429, 500, 502, 503, 504},
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(
            pool_connections=self.MAX_CONCURRENT_DOWNLOAD,
            pool_maxsize=self.MAX_CONCURRENT_DOWNLOAD,
            max_retries=retry_strategy,
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        # cleanup proxy if any
        proxies = {"http": "", "https": ""}
        session.proxies.update(proxies)

        # register the connection pool
        self.session = session
        self._proxy_set = False

    def shutdown(self):
        self.session.close()

    def configure_proxy(self, proxy: str):
        # configure proxy
        self._proxy_set = True
        proxies = {"http": proxy, "https": ""}
        self.session.proxies.update(proxies)

    def cleanup_proxy(self):
        self._proxy_set = False
        self.configure_proxy("")

    def _path_to_url(self, base: str, path: Union[Path, str]) -> str:
        # regulate base url, add suffix / to it if not existed
        base = f"{base}/" if not base.endswith("/") else base
        # convert to Path if path is str
        path = Path(path) if isinstance(path, str) else path

        relative_path = path
        # if the path is relative to /
        try:
            relative_path = path.relative_to("/")
        except ValueError:
            pass

        quoted_path = quote_from_bytes(bytes(relative_path))

        # switch scheme if needed
        _url_parsed = urlparse(urljoin(base, quoted_path))
        # unconditionally set scheme to HTTP if proxy is applied
        if self._proxy_set:
            _url_parsed = _url_parsed._replace(scheme="http")

        return _url_parsed.geturl()

    @partial(_retry, RETRY_COUNT, OUTER_BACKOFF_FACTOR, BACKOFF_MAX)
    def download(
        self,
        path: Union[Path, str],
        dst: Union[Path, str],
        digest: Optional[str] = None,
        size: Optional[int] = None,
        *,
        url_base: str,
        cookies: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> int:
        url = self._path_to_url(url_base, path)
        cookies = cookies if cookies else {}
        headers = headers if headers else {}
        dst = Path(dst)

        # specially deal with empty file
        if digest == self.EMPTY_STR_SHA256:
            dst.write_bytes(b"")
            return 0

        try:
            error_count = 0
            response = self.session.get(
                url, stream=True, cookies=cookies, headers=headers
            )

            raw_r = response.raw
            if raw_r.retries:
                error_count = len(raw_r.retries.history)
        except Exception as e:
            raise ExceedMaxRetryError(url, dst) from e

        hash_f = sha256()
        _downloaded_size = 0
        try:
            with open(dst, "wb") as f:
                for chunk in response.iter_content(chunk_size=self.CHUNK_SIZE):
                    hash_f.update(chunk)
                    f.write(chunk)
                    _downloaded_size += len(chunk)
        except (ChunkedEncodingError, ConnectionError, StreamConsumedError) as e:
            raise ChunkStreamingError(url, dst) from e
        except FileNotFoundError as e:
            raise DestinationNotAvailableError(url, dst) from e
        except OSError as e:
            # space not enough error
            if e.errno == errno.ENOSPC:
                raise DownloadFailedSpaceNotEnough(url, dst) from None

        # throw a network error if the file is not fully downloaded
        if size is not None and size != _downloaded_size:
            raise ChunkStreamingError(url, dst) from ValueError(
                "partial download detected"
            )

        calc_digest = hash_f.hexdigest()
        if digest and calc_digest != digest:
            msg = (
                f"hash check failed detected: "
                f"act={calc_digest}, exp={digest}, {url=}"
            )
            logger.error(msg)
            raise HashVerificaitonError(url, dst, msg)

        return error_count
