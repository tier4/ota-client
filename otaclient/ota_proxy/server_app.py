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
from http import HTTPStatus
from typing import Any, Dict, List, Union

from otaclient._utils.logging import BurstSuppressFilter
from .cache_control import OTAFileCacheControl
from .errors import BaseOTACacheError
from .ota_cache import OTACache

import logging

logger = logging.getLogger(__name__)
connection_err_logger = logging.getLogger(f"{__name__}.connection_err")
connection_err_logger.addFilter(
    BurstSuppressFilter(
        f"{__name__}.connection_err",
        upper_logger_name=__name__,
        burst_round_length=60,
        burst_max=6,
    )
)

# only expose app
__all__ = ("App",)


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
    def parse_raw_cookies(cookies_bytes: bytes) -> Dict[str, str]:
        """Parses raw cookies bytes into dict.

        Converts an input raw cookies string in bytes into a dict.

        Args:
            cookies_bytes bytes: raw cookies string in bytes
                i.e.: b"cpk0=cpv0;cpk1=cpv1;cpk2=cpv2"

        Returns:
            Dict[str, str]: dict represented cookies
        """
        cookie_pairs: List[str] = cookies_bytes.decode().split(";")
        res = dict()
        for p in cookie_pairs:
            k, v = p.strip().split("=")
            res[k] = v

        return res

    async def _respond_with_error(self, status: Union[HTTPStatus, int], msg: str, send):
        """Helper method for sending errors back to client"""
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    [b"content-type", b"text/html;charset=UTF-8"],
                ],
            }
        )
        await send({"type": "http.response.body", "body": msg.encode("utf8")})

    async def _send_chunk(self, data: bytes, more: bool, send):
        """Helper method for sending data chunks to client

        Args:
            data bytes
            more bool: whether there will be a next chunk or not
            send: ASGI send method
        """
        if more:
            await send({"type": "http.response.body", "body": data, "more_body": True})
        else:
            await send({"type": "http.response.body", "body": b""})

    async def _init_response(
        self, status: HTTPStatus, headers: List[Dict[str, Any]], send
    ):
        """Helper method for constructing and sending HTTP response back to client

        Args:
            status HTTPStatus
            headers dict: headers in the response
            send: ASGI send method
        """
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": headers,
            }
        )

    async def _pull_data_and_send(self, url: str, scope, send):
        """Streaming data between OTACache instance and ota_client

        Retrieves file descriptor from OTACache instance,
        yields chunks from file descriptor and streams chunks back to ota_client.

        Args:
            url str: URL requested by ota_client
            scope: ASGI scope for current request
            send: ASGI send method
        """
        # pass cookies and other headers needed for proxy into the ota_cache module
        cookies_dict: Dict[str, str] = dict()
        extra_headers: Dict[str, str] = dict()
        # currently we only need cookie and/or authorization header
        # also parse OTA-File-Cache-Control header
        ota_cache_control_policies = set()
        for header in scope["headers"]:
            if header[0] == b"cookie":
                cookies_dict = self.parse_raw_cookies(header[1])
            elif header[0] == b"authorization":
                extra_headers["Authorization"] = header[1].decode()
            # custome header for ota_file, see retrieve_file and OTACache for details
            elif header[0] == OTAFileCacheControl.header_lower.value.encode():
                try:
                    # NOTE: this statement can check over the received directives
                    ota_cache_control_policies = OTAFileCacheControl.parse_to_enum_set(
                        header[1].decode()
                    )
                    # also preserved the raw headers value string
                    extra_headers[OTAFileCacheControl.header.value] = header[1].decode()
                except KeyError:
                    await self._respond_with_error(
                        HTTPStatus.BAD_REQUEST,
                        f"bad OTA-File-Cache-Control headers: {header[1]}",
                        send,
                    )
                    return

        respond_started = False
        try:
            _bundle = await self._ota_cache.retrieve_file(
                url, cookies_dict, extra_headers, ota_cache_control_policies
            )
            if _bundle is None:
                await self._respond_with_error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    f"failed to retrieve fp for {url}",
                    send,
                )
                return
            fp, meta = _bundle

            # NOTE: currently only record content_type and content_encoding
            headers = []
            if meta.content_type:
                headers.append([b"Content-Type", meta.content_type.encode()])
            if meta.content_encoding:
                headers.append([b"Content-Encoding", meta.content_encoding.encode()])

            # prepare the response to the client
            await self._init_response(HTTPStatus.OK, headers, send)
            respond_started = True

            # stream the response to the client
            async for chunk in fp:
                await self._send_chunk(chunk, True, send)
            # finish the streaming by send a 0 len payload
            await self._send_chunk(b"", False, send)

        except aiohttp.ClientResponseError as e:
            logger.error(f"request for {url=} failed due to HTTP error: {e!r}")
            # passthrough 4xx(currently 403 and 404) to otaclient
            await self._respond_with_error(e.status, e.message, send)
        except aiohttp.ClientConnectionError as e:
            connection_err_logger.error(
                f"request for {url=} failed due to connection error: {e!r}"
            )
            if respond_started:
                await send({"type": "http.response.body", "body": b""})
            else:
                await self._respond_with_error(
                    HTTPStatus.BAD_GATEWAY,
                    "failed to connect to remote server",
                    send,
                )
        except aiohttp.ClientError as e:
            logger.error(
                f"request for {url=} failed due to aiohttp client error: {e!r}"
            )
            # terminate the transmission
            if respond_started:
                await send({"type": "http.response.body", "body": b""})
            else:
                await self._respond_with_error(
                    HTTPStatus.SERVICE_UNAVAILABLE, f"client error: {e!r}", send
                )
        except (BaseOTACacheError, StopAsyncIteration) as e:
            logger.error(
                f"request for {url=} failed due to handled ota_cache internal error: {e!r}"
            )
            # terminate the transmission
            if respond_started:
                await send({"type": "http.response.body", "body": b""})
            else:
                await self._respond_with_error(
                    HTTPStatus.INTERNAL_SERVER_ERROR, f"internal error: {e!r}", send
                )
        except Exception as e:
            # exceptions rather than aiohttp error indicates
            # internal errors of ota_cache
            logger.exception(
                f"request for {url=} failed due to unhandled ota_cache internal error: {e!r}"
            )
            # terminate the transmission
            if respond_started:
                await send({"type": "http.response.body", "body": b""})
            else:
                await self._respond_with_error(
                    HTTPStatus.INTERNAL_SERVER_ERROR, f"internal error: {e!r}", send
                )

    async def app(self, scope, send):
        """The real entry for the ota_proxy."""
        from urllib.parse import urlparse

        assert scope["type"] == "http"
        # check method, currently only support GET method
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
        else:
            # real app entry
            await self.app(scope, send)
