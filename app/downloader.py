import requests
import time
from functools import partial
from hashlib import sha256
from pathlib import Path
from typing import Dict, Optional, Union
from urllib.parse import quote_from_bytes, urljoin, urlparse

from app.configs import OTAFileCacheControl, config as cfg
from app.ota_error import OtaErrorRecoverable

from app import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class _ExceptionWrapper(Exception):
    pass


def _retry(retry, backoff_factor, backoff_max, func):
    """simple retrier"""
    from functools import wraps

    @wraps(func)
    def _wrapper(*args, **kwargs):
        _retry_count, _retry_cache = 0, False
        try:
            while True:
                try:
                    if _retry_cache:
                        # add a Ota-File-Cache-Control header to indicate ota_proxy
                        # to re-cache the possible corrupted file.
                        # modify header if needed and inject it into kwargs
                        if "headers" in kwargs:
                            kwargs["headers"].update(
                                {
                                    OTAFileCacheControl.header_lower.value: OTAFileCacheControl.retry_caching.value
                                }
                            )
                        else:
                            kwargs["headers"] = {
                                OTAFileCacheControl.header_lower.value: OTAFileCacheControl.retry_caching.value
                            }

                    # inject headers
                    return func(*args, **kwargs)
                except _ExceptionWrapper as e:
                    # unwrap exception
                    _inner_e = e.__cause__
                    _retry_count += 1

                    if _retry_count > retry:
                        raise
                    else:
                        # special case: hash calculation error detected,
                        # might indicate corrupted cached files
                        if isinstance(_inner_e, ValueError):
                            _retry_cache = True

                        _backoff_time = float(
                            min(backoff_max, backoff_factor * (2 ** (_retry_count - 1)))
                        )
                        time.sleep(_backoff_time)

        except _ExceptionWrapper as e:
            # currently all exceptions lead to OtaErrorRecoverable
            _inner_e = e.__cause__
            _url = e.args[0]
            raise OtaErrorRecoverable(
                f"failed after {_retry_count} tries for {_url}: {_inner_e!r}"
            )

    return _wrapper


class Downloader:
    CHUNK_SIZE = cfg.CHUNK_SIZE
    RETRY_COUNT = cfg.DOWNLOAD_RETRY
    BACKOFF_FACTOR = 1
    OUTER_BACKOFF_FACTOR = 0.01
    BACKOFF_MAX = cfg.DOWNLOAD_BACKOFF_MAX
    MAX_CONCURRENT_DOWNLOAD = cfg.MAX_CONCURRENT_DOWNLOAD

    def __init__(self):
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        # base session
        session = requests.Session()

        # cleanup proxy if any
        self._proxy_set = False
        proxies = {"http": "", "https": ""}
        session.proxies.update(proxies)

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

        # register the connection pool
        self._session = session

    def shutdown(self):
        self._session.close()

    def configure_proxy(self, proxy: str):
        # configure proxy
        self._proxy_set = True
        proxies = {"http": proxy, "https": ""}
        self._session.proxies.update(proxies)

    def cleanup_proxy(self):
        self._proxy_set = False
        self.configure_proxy("")

    def _path_to_url(self, base: str, path: Union[Path, str]) -> str:
        # regulate base url, add suffix / to it if not existed
        if not base.endswith("/"):
            base = f"{base}/"

        if isinstance(path, str):
            path = Path(path)

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
        digest: Optional[str],
        *,
        url_base: str,
        cookies: Dict[str, str],
        headers: Optional[Dict[str, str]] = None,
    ) -> int:
        url = self._path_to_url(url_base, path)
        if not headers:
            headers = dict()

        if isinstance(dst, str):
            dst = Path(dst)

        # specially deal with empty file
        if digest == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855":
            dst.write_bytes(b"")
            return 0

        try:
            error_count = 0
            response = self._session.get(
                url, stream=True, cookies=cookies, headers=headers
            )
            response.raise_for_status()

            raw_r = response.raw
            if raw_r.retries:
                error_count = len(raw_r.retries.history)

            # prepare hash
            hash_f = sha256()
            with open(dst, "wb") as f:
                for data in response.iter_content(chunk_size=self.CHUNK_SIZE):
                    hash_f.update(data)
                    f.write(data)

            calc_digest = hash_f.hexdigest()
            if digest and calc_digest != digest:
                msg = f"hash check failed detected: act={calc_digest}, exp={digest}, {url=}"
                logger.error(msg)
                raise ValueError(msg)
        except Exception as e:
            # rewrap the exception with url
            raise _ExceptionWrapper(url) from e

        return error_count
