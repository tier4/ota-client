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
import hashlib
import logging
from abc import abstractmethod
from functools import wraps
from http import HTTPStatus
from typing import (
    IO,
    Any,
    ByteString,
    Callable,
    Iterator,
    Mapping,
    Optional,
    Protocol,
    Union,
)
from urllib.parse import urlsplit

import requests
import requests.exceptions
import zstandard
from requests.structures import CaseInsensitiveDict as CIDict

from ota_proxy import OTAFileCacheControl
from otaclient.app.ota_client_stub import T
from otaclient_common.typing import P, StrOrPath

logger = logging.getLogger(__name__)

EMPTY_FILE_SHA256 = r"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
CACHE_CONTROL_HEADER = OTAFileCacheControl.HEADER_LOWERCASE

# ------ errors definition ------ #


class DownloaderShutdownedError(Exception):
    """Raised when try to use closed downloader."""


class DownloadError(Exception):
    """Basic Download error.

    These errors are exception that should be handled by the caller.
    Other errors raised by requests can simply be handled by outer retry.
    """


class PartialDownloaded(Exception):
    """Download is not completed."""


class UnhandledHTTPError(DownloadError):
    """HTTPErrors that cannot be handled by us.

    Currently include 401, 403 and 404.
    """


class SaveFileFailed(DownloadError):
    pass


class HashVerificaitonError(DownloadError):
    pass


class SpaceNotEnough(DownloadError):
    pass


# ------ decompression support ------ #
# Currently we support zstd compression.


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


# ------ OTA cache control implementation ------ #


def _exception_handler(func: Callable[P, T]) -> Callable[P, T]:
    @wraps(func)
    def _wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return func(*args, **kwargs)
        except HashVerificaitonError:
            # inject a OTA-File-Cache-Control header to indicate ota_proxy
            # to re-cache the possible corrupted file.
            # modify header if needed and inject it into kwargs
            parsed_header: dict[str, str] = {}

            prepared_headers = kwargs.pop("headers", {})
            if isinstance(prepared_headers, Mapping):
                parsed_header.update(prepared_headers)

            # preserve the already set policies, while add retry_caching policy
            _cache_policy = parsed_header.pop(CACHE_CONTROL_HEADER, "")
            _cache_policy = OTAFileCacheControl.update_header_str(
                _cache_policy, retry_caching=True
            )
            parsed_header[CACHE_CONTROL_HEADER] = _cache_policy

            # replace with updated header
            kwargs["headers"] = parsed_header

            # try ONCE with headers included OTA cache control retry_caching directory,
            # if still failed, let the outer retrier does its job.
            return func(*args, **kwargs)
        except requests.exceptions.HTTPError as e:
            http_errcode = e.errno
            if (
                http_errcode == HTTPStatus.FORBIDDEN
                or http_errcode == HTTPStatus.NOT_FOUND
                or http_errcode == HTTPStatus.UNAUTHORIZED
            ):
                raise UnhandledHTTPError from e
            raise  # re-raise to upper caller for other exceptions
        # exception group 5: file saving location not available
        except FileNotFoundError as e:
            raise SaveFileFailed from e
        # exception group 6: Disk out-of-space
        except OSError as e:
            if e.errno == errno.ENOSPC:
                raise SpaceNotEnough from e
            raise  # re-raise to upper caller for other exceptions

    return _wrapper


# ------ downloader implementation ------ #


class Downloader:
    """A downloader implementation with requests.

    NOTE: this Downloader can not be used in multi-thread!
    """

    def __init__(
        self,
        *,
        chunk_size: int,
        hash_func: Callable[..., hashlib._Hash],
        use_http_if_http_proxy_set: bool = True,
        cookies: dict[str, str] | None = None,
        proxies: dict[str, str] | None = None,
    ) -> None:
        self.chunk_size = chunk_size
        self.hash_func = hash_func

        self._proxies: dict[str, str] = cookies.copy() if cookies else {}
        self._cookies: dict[str, str] = proxies.copy() if proxies else {}
        self.use_http_if_http_proxy_set = use_http_if_http_proxy_set

        # downloading stats collecting
        self._downloaded_bytes = 0
        # ------ setup the requests.Session ------ #
        self._session = requests.Session()

        # ------ compression support ------ #
        self._compression_support_matrix = {}
        # zstd decompression adapter
        zstd_decompressor = ZstdDecompressionAdapter()
        self._compression_support_matrix["zst"] = zstd_decompressor
        self._compression_support_matrix["zstd"] = zstd_decompressor

    def _get_decompressor(
        self, compression_alg: Any
    ) -> Optional[DecompressionAdapterProtocol]:
        """Get thread-local private decompressor adapter accordingly."""
        return self._compression_support_matrix.get(compression_alg)

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
        dst: StrOrPath,
        digest: Optional[str],
        compression_alg: Optional[str],
        resp_headers: CIDict,
    ) -> tuple[Optional[str], Optional[str]]:
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

    def _prepare_url(self, url: str, proxies: Optional[dict[str, str]] = None) -> str:
        """Force changing URL scheme to HTTP if required.

        When upper otaproxy is set and use_http_if_http_proxy_set is True,
            Force changing the URL scheme to HTTP.
        """
        if self.use_http_if_http_proxy_set and proxies and "http" in proxies:
            return urlsplit(url)._replace(scheme="http").geturl()
        return url

    # API

    @property
    def downloaded_bytes(self) -> int:
        return self._downloaded_bytes

    @_exception_handler
    def download(
        self,
        url: str,
        dst: StrOrPath,
        *,
        size: int | None = None,
        digest: str | None = None,
        headers: dict[str, str] | None = None,
        compression_alg: str | None = None,
    ) -> tuple[int, int]:
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
        cookies = self._cookies

        prepared_url = self._prepare_url(url, proxies)
        # NOTE: process headers AFTER proxies setting is parsed
        prepared_headers = self._prepare_header(
            headers,
            digest=digest,
            compression_alg=compression_alg,
            proxies=proxies,
        )
        digestobj = self.hash_func()
        downloaded_file_size, traffic_on_wire = 0, 0

        with self._session.get(
            prepared_url,
            stream=True,
            proxies=proxies,
            cookies=cookies,
            headers=prepared_headers,
        ) as resp, open(dst, "wb") as dst_fp:
            resp.raise_for_status()

            digest, compression_alg = self._check_against_cache_policy_in_resp(
                url,
                dst,
                digest,
                compression_alg,
                resp.headers,
            )

            raw_resp = resp.raw  # the underlaying urllib3 Response object
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

        # checking the download result
        if size and size != downloaded_file_size:
            _err_msg = (
                f"detect partial downloading: {size=} != {downloaded_file_size=} for "
                f"{prepared_url}, saving to {dst}"
            )
            raise PartialDownloaded(_err_msg)

        if digest and ((calc_digest := digestobj.hexdigest()) != digest):
            _err_msg = f"hash verification failed: {digest=} != {calc_digest=} for {prepared_url}"
            raise HashVerificaitonError(_err_msg)

        return downloaded_file_size, traffic_on_wire
