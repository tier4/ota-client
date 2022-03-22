from http import HTTPStatus
from threading import Lock
from typing import Dict, List

import aiohttp

from . import ota_cache
from .config import OTAFileCacheControl, config as cfg

import logging

logger = logging.getLogger(__name__)
logger.setLevel(cfg.LOG_LEVEL)

# only expose app
__all__ = "App"


class App:
    """The ASGI application for ota_proxy server passed to unvicorn.

    The App will initialize an instance of OTACache on its initializing.
    It is responsible for requets proxy between ota_client(local or subECUs),
    streaming data between OTACache and ota_clients.

    NOTE:
        a. This App only support plain HTTP request proxy(CONNECT method is not supported).

        b. It seems that uvicorn will not interrupt the App running even the client closes connection.

    Attributes:
        kwargs dict[str, Any]: App doesn't use any of the kwargs,
            these kwargs will be passed through to ota_cache directly for initializing.
            Check ota_cache.OTACache for details.

    Example usage:
        # initialize an instance of the App:
        app = App(cache_enabled=True, init_cache=False, enable_https=False)
        # load the app with uvicorn, and start uvicorn
        # NOTE: lifespan must be set to "on" for properly close the ota_proxy server
        uvicorn.run(app, host="0.0.0.0", port=8082, log_level="debug", lifespan="on")
    """

    def __init__(self, **kwargs):
        # passthrough all kwargs to the ota_cache initializing
        self._kwargs = kwargs
        self.started = False
        self._lock = Lock()

    def start(self):
        """Inits and starts the OTACache instance.

        OTACache will be initialized and launched in the background.
        NOTE: if there are multiple calls on this method, only one call will be executed,
        other calls will be ignored sliently
        """
        if self._lock.acquire(blocking=False) and not self.started:
            logger.info("start ota http proxy app...")
            self.started = True
            self._ota_cache = ota_cache.OTACache(**self._kwargs)

            self._lock.release()

    def stop(self):
        """Closes the OTACache instance.

        NOTE: if there are multiple calls on this method, only one call will be executed,
        other calls will be ignored sliently
        """
        if self._lock.acquire(blocking=False) and self.started:
            logger.info("stopping ota http proxy app...")
            self._ota_cache.close()
            logger.info("shutdown server completed")
            self.started = False

            self._lock.release()

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

    async def _respond_with_error(self, status: HTTPStatus, msg: str, send):
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

    async def _init_response(self, status: HTTPStatus, headers: dict, send):
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

        f: ota_cache.OTAFile = None
        try:
            f = await self._ota_cache.retrieve_file(
                url, cookies_dict, extra_headers, ota_cache_control_policies
            )
        except aiohttp.ClientResponseError as e:
            await self._respond_with_error(e.status, e.message, send)
            raise
        except aiohttp.ClientConnectionError:
            await self._respond_with_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "failed to connect to remote server",
                send,
            )
            raise
        except aiohttp.ClientError as e:
            await self._respond_with_error(
                HTTPStatus.INTERNAL_SERVER_ERROR, f"client error: {e!r}", send
            )
            raise
        except Exception as e:
            await self._respond_with_error(
                HTTPStatus.INTERNAL_SERVER_ERROR, f"{e!r}", send
            )
            raise

        # parse response
        # NOTE: currently only record content_type and content_encoding
        if f:
            meta = f.meta
            headers = []
            if meta.content_type:
                headers.append([b"Content-Type", meta.content_type.encode()])
            if meta.content_encoding:
                headers.append([b"Content-Encoding", meta.content_encoding.encode()])

            # prepare the response to the client
            await self._init_response(HTTPStatus.OK, headers, send)

            # stream the response to the client
            async for chunk in f.get_chunks():
                await self._send_chunk(chunk, True, send)
            # finish the streaming by send a 0 len payload
            await self._send_chunk(b"", False, send)
        else:
            await self._respond_with_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"failed to retrieve file for {url=}",
                send,
            )
            raise ValueError(f"failed to retrieve {url=} from ota_cache")

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
                    self.start()
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    self.stop()
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        else:
            # real app entry
            await self.app(scope, send)
