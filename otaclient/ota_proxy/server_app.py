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


import asyncio
import aiohttp
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Dict, List, Tuple, Union
from urllib.parse import urlparse

from otaclient._utils.logging import BurstSuppressFilter
from .errors import BaseOTACacheError
from .ota_cache import OTACache

import logging

logger = logging.getLogger(__name__)
connection_err_logger = logging.getLogger(f"{__name__}.connection_err")
# NOTE: for connection_error, only allow max 6 lines of logging per 30 seconds
connection_err_logger.addFilter(
    BurstSuppressFilter(
        f"{__name__}.connection_err",
        upper_logger_name=__name__,
        burst_round_length=30,
        burst_max=6,
    )
)

# only expose app
__all__ = ("App",)


# uvicorn HTTP resp type
TYPE_RESP_BODY = "http.response.body"
TYPE_RESP_START = "http.response.start"


# helper methods


def decode_raw_headers(raw_headers: List[Tuple[bytes, bytes]]) -> Dict[str, str]:
    """Decode raw headers from client's request."""
    headers: Dict[str, str] = {}
    for raw_header in raw_headers:
        if len(raw_header) != 2 or not raw_header[-1]:
            continue
        hname, hvalue = raw_header
        headers[hname.decode()] = hvalue.decode()
    return headers


def encode_headers(headers: Dict[str, str]) -> List[Tuple[bytes, bytes]]:
    raw_headers: List[Tuple[bytes, bytes]] = []
    for hname, hvalue in headers.items():
        if not (hvalue and isinstance(hvalue, str)):
            continue
        raw_headers.append((hname.lower().encode(), hvalue.encode()))
    return raw_headers


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
        ota_cache: initialized but not yet launched ota_cache instance

    Example usage:

        # initialize an instance of the App:
        _ota_cache = OTACache(cache_enabled=True, init_cache=False, enable_https=False)
        app = App(_ota_cache)

        # load the app with uvicorn, and start uvicorn
        # NOTE: lifespan must be set to "on" for properly launching/closing ota_cache instance
        uvicorn.run(app, host="0.0.0.0", port=8082, log_level="debug", lifespan="on")
    """

    def __init__(self, ota_cache: OTACache):
        self._lock = asyncio.Lock()
        self._closed = True
        self._ota_cache = ota_cache

    async def start(self):
        """Start the ota_cache instance."""
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
    async def _respond_with_error(status: Union[HTTPStatus, int], msg: str, send):
        """Helper method for sending errors back to client"""
        await send(
            {
                "type": TYPE_RESP_START,
                "status": status,
                "headers": [
                    [b"content-type", b"text/html;charset=UTF-8"],
                ],
            }
        )
        await send({"type": TYPE_RESP_BODY, "body": msg.encode("utf8")})

    @staticmethod
    async def _send_chunk(data: bytes, more: bool, send):
        """Helper method for sending data chunks to client

        Args:
            data bytes
            more bool: whether there will be a next chunk or not
            send: ASGI send method
        """
        if more:
            await send({"type": TYPE_RESP_BODY, "body": data, "more_body": True})
        else:
            await send({"type": TYPE_RESP_BODY, "body": b""})

    @staticmethod
    async def _init_response(
        status: Union[HTTPStatus, int], headers: List[Tuple[bytes, bytes]], send
    ):
        """Helper method for constructing and sending HTTP response back to client

        Args:
            status HTTPStatus
            headers dict: headers in the response
            send: ASGI send method
        """
        await send(
            {
                "type": TYPE_RESP_START,
                "status": status,
                "headers": headers,
            }
        )

    @asynccontextmanager
    async def _error_handling_for_cache_retrieving(self, url: str, send):
        _common_err_msg = f"request for {url=} failed"
        try:
            yield
        except aiohttp.ClientResponseError as e:
            logger.error(f"{_common_err_msg} due to HTTP error: {e!r}")
            # passthrough 4xx(currently 403 and 404) to otaclient
            await self._respond_with_error(e.status, e.message, send)
        except aiohttp.ClientConnectionError as e:
            connection_err_logger.error(
                f"{_common_err_msg} due to connection error: {e!r}"
            )
            await self._respond_with_error(
                HTTPStatus.BAD_GATEWAY,
                "failed to connect to remote server",
                send,
            )
        except aiohttp.ClientError as e:
            logger.error(f"{_common_err_msg} due to aiohttp client error: {e!r}")
            await self._respond_with_error(
                HTTPStatus.SERVICE_UNAVAILABLE, f"client error: {e!r}", send
            )
        except (BaseOTACacheError, StopAsyncIteration) as e:
            logger.error(
                f"{_common_err_msg} due to handled ota_cache internal error: {e!r}"
            )
            await self._respond_with_error(
                HTTPStatus.INTERNAL_SERVER_ERROR, f"internal error: {e!r}", send
            )
        except Exception as e:
            # exceptions rather than aiohttp error indicates
            # internal errors of ota_cache
            logger.exception(
                f"{_common_err_msg} due to unhandled ota_cache internal error: {e!r}"
            )
            await self._respond_with_error(
                HTTPStatus.INTERNAL_SERVER_ERROR, f"internal error: {e!r}", send
            )

    @asynccontextmanager
    async def _error_handling_during_transferring(self, url: str, send):
        """
        NOTE: for exeception during transferring, only thing we can do is to
              terminate the transfer by sending empty chunk back to otaclient.
        """
        _common_err_msg = f"request for {url=} failed"
        try:
            yield
        except (BaseOTACacheError, StopAsyncIteration) as e:
            logger.error(
                f"{_common_err_msg=} due to handled ota_cache internal error: {e!r}"
            )
            await self._send_chunk(b"", False, send)
        except Exception as e:
            # unexpected internal errors of ota_cache
            logger.exception(
                f"{_common_err_msg=} due to unhandled ota_cache internal error: {e!r}"
            )
            await self._send_chunk(b"", False, send)

    async def _pull_data_and_send(self, url: str, scope, send):
        """Streaming data between OTACache instance and ota_client

        Retrieves file descriptor from OTACache instance,
        yields chunks from file descriptor and streams chunks back to ota_client.

        Args:
            url str: URL requested by ota_client
            scope: ASGI scope for current request
            send: ASGI send method
        """
        headers_from_client = decode_raw_headers(scope["headers"])

        # try to get a cache entry for this URL or for file_sha256 indicated by cache_policy
        async with self._error_handling_for_cache_retrieving(url, send):
            _bundle = await self._ota_cache.retrieve_file(url, headers_from_client)
            if _bundle is None:
                await self._respond_with_error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    f"failed to retrieve fp for {url}",
                    send,
                )
                return
            fp, headers_to_client = _bundle

        async with self._error_handling_during_transferring(url, send):
            await self._init_response(
                HTTPStatus.OK, encode_headers(headers_to_client), send
            )
            async for chunk in fp:
                await self._send_chunk(chunk, True, send)
            await self._send_chunk(b"", False, send)

    async def http_handler(self, scope, send):
        """The real entry for the ota_proxy."""
        if scope["method"] != "GET":
            msg = "ONLY SUPPORT GET METHOD."
            await self._respond_with_error(HTTPStatus.BAD_REQUEST, msg, send)
            return

        # get the url from the request
        url = scope["path"]
        _url = urlparse(url)
        if not _url.scheme or not _url.path:
            msg = f"INVALID URL {url}."
            await self._respond_with_error(HTTPStatus.BAD_REQUEST, msg, send)
            return

        logger.debug(f"receive request for {url=}")
        await self._pull_data_and_send(url, scope, send)

    async def __call__(self, scope, receive, send):
        """The entrance of the ASGI application.

        This method directly handles the income requests.
        It filters requests, hands valid requests over to the app entry,
        and handles lifespan protocol to start/stop server properly.
        """
        if scope["type"] == "lifespan":
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
        elif scope["type"] == "http":
            await self.http_handler(scope, send)
        # ignore unknown request type
