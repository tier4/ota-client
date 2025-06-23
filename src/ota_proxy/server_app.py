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
from http import HTTPStatus
from urllib.parse import urlparse

import aiohttp
from multidict import CIMultiDict, CIMultiDictProxy

from otaclient_common._logging import get_burst_suppressed_logger

from ._consts import (
    BHEADER_AUTHORIZATION,
    BHEADER_CONTENT_ENCODING,
    BHEADER_CONTENT_TYPE,
    BHEADER_COOKIE,
    BHEADER_OTA_FILE_CACHE_CONTROL,
    HEADER_AUTHORIZATION,
    HEADER_CONTENT_ENCODING,
    HEADER_CONTENT_TYPE,
    HEADER_COOKIE,
    HEADER_OTA_FILE_CACHE_CONTROL,
    METHOD_GET,
    REQ_TYPE_HTTP,
    REQ_TYPE_LIFESPAN,
    RESP_TYPE_BODY,
    RESP_TYPE_START,
)
from .config import config as cfg
from .errors import BaseOTACacheError, CacheProviderNotReady, ReaderPoolBusy
from .ota_cache import OTACache

logger = logging.getLogger(__name__)
# NOTE: for request_error, only allow max 6 lines of logging per 30 seconds
burst_suppressed_logger = get_burst_suppressed_logger(f"{__name__}.request_error")

# only expose app
__all__ = ("App",)

# helper methods


def parse_raw_headers(raw_headers: list[tuple[bytes, bytes]]) -> CIMultiDict[str]:
    """Picks and decode raw headers from client's request.

    Uvicorn sends headers from client's request to application as list of encoded bytes tuple.
    Uvicorn will process the headers name into lower case.
    Currently we only need authorization, cookie and ota-file-cache-control header.

    Returns:
        An inst of header dict.
    """
    headers = CIMultiDict()
    for bheader_pair in raw_headers:
        if len(bheader_pair) != 2:
            continue
        bname, bvalue = bheader_pair
        if bname == BHEADER_AUTHORIZATION:
            headers[HEADER_AUTHORIZATION] = bvalue.strip().decode(
                "utf-8", "surrogateescape"
            )
        elif bname == BHEADER_COOKIE:
            headers[HEADER_COOKIE] = bvalue.strip().decode("utf-8", "surrogateescape")
        elif bname == BHEADER_OTA_FILE_CACHE_CONTROL:
            headers[HEADER_OTA_FILE_CACHE_CONTROL] = bvalue.decode(
                "utf-8", "surrogateescape"
            )
    return headers


def encode_headers(
    headers: CIMultiDict[str] | CIMultiDictProxy[str],
) -> list[tuple[bytes, bytes]]:
    """Encode headers dict to list of bytes tuples for sending back to client.

    Uvicorn requests application to pre-process headers to bytes.
    Currently we send the following headers back to client:
    1. content-encoding
    2. content-type
    3. ota-file-cache-control header
    """
    bytes_headers: list[tuple[bytes, bytes]] = []
    if _encoding := headers.get(HEADER_CONTENT_ENCODING):
        bytes_headers.append((BHEADER_CONTENT_ENCODING, _encoding.encode("utf-8")))
    if _type := headers.get(HEADER_CONTENT_TYPE):
        bytes_headers.append((BHEADER_CONTENT_TYPE, _type.encode("utf-8")))
    if _cache_control := headers.get(HEADER_OTA_FILE_CACHE_CONTROL):
        bytes_headers.append(
            (BHEADER_OTA_FILE_CACHE_CONTROL, _cache_control.encode("utf-8"))
        )
    return bytes_headers


# uvicorn APP


class App:
    """The ASGI application for ota_proxy server passed to unvicorn.

    The App will initialize an instance of OTACache on its initializing.
    It is responsible for requets proxy between ota_client(local or subECUs),
    streaming data between OTACache and ota_clients.

    NOTE:
        a. This App only support plain HTTP request proxy(CONNECT method is not supported).

        b. It seems that uvicorn will not interrupt the App running even the client closes connection.

    Attributes:
        ota_cache: initialized but not yet launched ota_cache instance.

    Example usage:

        # initialize an instance of the App:

        _ota_cache = OTACache(...)
        app = App(_ota_cache)

        # load the app with uvicorn, and start uvicorn
        # NOTE: lifespan must be set to "on" for properly launching/closing ota_cache instance

        uvicorn.run(app, host="0.0.0.0", port=8082, log_level="debug", lifespan="on")
    """

    def __init__(
        self,
        ota_cache: OTACache,
        *,
        max_concurrent_requests: int = cfg.MAX_CONCURRENT_REQUESTS,
    ):
        self._lock = asyncio.Lock()
        self._closed = True
        self._ota_cache = ota_cache

        self.max_concurrent_requests = max_concurrent_requests
        self._se = asyncio.Semaphore(max_concurrent_requests)

    async def start(self):
        """Start the ota_cache instance."""
        logger.info("server started")
        logger.info(f"Event loop policy: {asyncio.get_event_loop_policy()=}")
        logger.info(f"Event loop: {asyncio.get_event_loop()=}")

        async with self._lock:
            if self._closed:
                self._closed = False
                await self._ota_cache.start()

    async def stop(self):
        """Stop the ota_cache instance."""
        async with self._lock:
            if not self._closed:
                self._closed = True
                await self._ota_cache.close()

    @staticmethod
    async def _respond_with_error(status: HTTPStatus | int, msg: str, send):
        """Helper method for sending errors back to client."""
        await send(
            {
                "type": RESP_TYPE_START,
                "status": status,
                "headers": [
                    [b"content-type", b"text/html;charset=UTF-8"],
                ],
            }
        )
        await send({"type": RESP_TYPE_BODY, "body": msg.encode("utf8")})

    @staticmethod
    async def _stream_send_chunk(data: bytes, send):
        """Helper method for sending data chunks to client.

        Args:
            data: bytes to send to client.
            more bool: whether there will be a next chunk or not.
            send: ASGI send method.
        """
        await send({"type": RESP_TYPE_BODY, "body": data, "more_body": True})

    @staticmethod
    async def _send_chunk_one(data: bytes, send):
        """Send only one chunk of data and finish."""
        await send({"type": RESP_TYPE_BODY, "body": data})

    @staticmethod
    async def _init_response(
        status: HTTPStatus | int, headers: list[tuple[bytes, bytes]], send
    ):
        """Helper method for constructing and sending HTTP response back to client.

        Args:
            status: HTTP status code for this response.
            headers dict: headers to be sent in this response.
            send: ASGI send method.
        """
        await send(
            {
                "type": RESP_TYPE_START,
                "status": status,
                "headers": headers,
            }
        )

    async def _error_handling_for_cache_retrieving(
        self, exc: Exception, url: str, send
    ) -> None:
        try:
            _common_err_msg = f"request for {url=} failed"
            if isinstance(exc, (ReaderPoolBusy, CacheProviderNotReady)):
                _err_msg = f"{_common_err_msg} due to otaproxy is busy: {exc!r}"
                burst_suppressed_logger.error(_err_msg)
                await self._respond_with_error(
                    HTTPStatus.SERVICE_UNAVAILABLE, "otaproxy internal busy", send
                )
            elif isinstance(exc, aiohttp.ClientResponseError):
                _err_msg = f"{_common_err_msg} due to HTTP error: {exc!r}"
                burst_suppressed_logger.error(_err_msg)
                # passthrough 4xx(currently 403 and 404) to otaclient
                await self._respond_with_error(
                    exc.status, f"HTTP status from remote: {exc.status}", send
                )
            elif isinstance(exc, aiohttp.ClientConnectionError):
                _err_msg = f"{_common_err_msg} due to connection error: {exc!r}"
                burst_suppressed_logger.error(_err_msg)
                await self._respond_with_error(
                    HTTPStatus.BAD_GATEWAY, "Failed to connect to remote", send
                )
            elif isinstance(exc, aiohttp.ClientError):
                _err_msg = f"{_common_err_msg} due to aiohttp client error: {exc!r}"
                burst_suppressed_logger.error(_err_msg)
                await self._respond_with_error(
                    HTTPStatus.SERVICE_UNAVAILABLE, "HTTP client error", send
                )
            elif isinstance(exc, (BaseOTACacheError, StopAsyncIteration)):
                _err_msg = f"{_common_err_msg} due to handled ota_cache internal error: {exc!r}"
                burst_suppressed_logger.error(_err_msg)
                await self._respond_with_error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "otaproxy internal error",
                    send,
                )
            else:
                # exceptions rather than aiohttp error indicates
                # internal errors of ota_cache
                _err_msg = f"{_common_err_msg} due to unhandled ota_cache internal error: {exc!r}"
                burst_suppressed_logger.exception(_err_msg)
                await self._respond_with_error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "unhandled internal server error",
                    send,
                )
        finally:
            del exc  # break ref

    async def _error_handling_during_transferring(
        self, exc: Exception, url: str, send
    ) -> None:
        """
        NOTE: for exeception during transferring, only thing we can do is to
              terminate the transfer by sending empty chunk back to otaclient.
        """
        try:
            _common_err_msg = f"request for {url=} failed"
            if isinstance(exc, (BaseOTACacheError, StopAsyncIteration)):
                burst_suppressed_logger.error(
                    f"{_common_err_msg=} due to handled ota_cache internal error: {exc!r}"
                )
                await self._send_chunk_one(b"", send)
            else:
                # unexpected internal errors of ota_cache
                burst_suppressed_logger.error(
                    f"{_common_err_msg=} due to unhandled ota_cache internal error: {exc!r}"
                )
                await self._send_chunk_one(b"", send)
        finally:
            del exc  # break ref

    async def _pull_data_and_send(self, url: str, scope, send):
        """Streaming data between OTACache instance and ota_client

        Retrieves file descriptor from OTACache instance,
        yields chunks from file descriptor and streams chunks back to ota_client.

        Args:
            url str: URL requested by ota_client.
            scope: ASGI scope for current request.
            send: ASGI send method.
        """
        headers_from_client = parse_raw_headers(scope["headers"])

        # try to get a cache entry for this URL or for file_sha256 indicated by cache_policy
        retrieved_ota_cache = None
        try:
            retrieved_ota_cache = await self._ota_cache.retrieve_file(
                url, headers_from_client
            )
        except Exception as e:
            await self._error_handling_for_cache_retrieving(e, url, send)
            return

        if retrieved_ota_cache is None:
            # retrieve_file executed successfully, but return nothing
            _msg = f"failed to retrieve fd for {url} from otacache"
            burst_suppressed_logger.warning(_msg)
            await self._respond_with_error(HTTPStatus.INTERNAL_SERVER_ERROR, _msg, send)
            return

        fp, headers_to_client = retrieved_ota_cache
        await self._init_response(
            HTTPStatus.OK, encode_headers(headers_to_client), send
        )
        try:
            if isinstance(fp, bytes):
                await self._send_chunk_one(fp, send)
            else:
                async for chunk in fp:
                    await self._stream_send_chunk(chunk, send)
                await self._send_chunk_one(b"", send)
        except Exception as e:
            await self._error_handling_during_transferring(e, url, send)
            return

    async def http_handler(self, scope, send):
        """The real entry for the ota_proxy."""
        if scope["method"] != METHOD_GET:
            msg = "ONLY SUPPORT GET METHOD."
            await self._respond_with_error(HTTPStatus.BAD_REQUEST, msg, send)
            return

        url = scope["path"]
        _url = urlparse(url)
        if not _url.scheme or not _url.path:
            msg = f"INVALID URL {url}."
            await self._respond_with_error(HTTPStatus.BAD_REQUEST, msg, send)
            return

        if self._se.locked():
            burst_suppressed_logger.warning(
                f"exceed max pending requests: {self.max_concurrent_requests}, respond with 429"
            )
            await self._respond_with_error(HTTPStatus.TOO_MANY_REQUESTS, "", send)
            return

        await self._se.acquire()
        try:
            await self._pull_data_and_send(url, scope, send)
        finally:
            self._se.release()

    async def __call__(self, scope, receive, send):
        """The entrance of the ASGI application.

        This method directly handles the income requests.
        It filters requests, hands valid requests over to the app entry,
        and handles lifespan protocol to start/stop server properly.
        """
        if scope["type"] == REQ_TYPE_LIFESPAN:
            # handling lifespan protocol
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await self.start()
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await self.stop()
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        elif scope["type"] == REQ_TYPE_HTTP:
            await self.http_handler(scope, send)
        # ignore unknown request type
