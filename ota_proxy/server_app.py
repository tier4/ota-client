from http import HTTPStatus
from threading import Lock

from . import ota_cache

import logging

logger = logging.getLogger(__name__)

# only expose app
__all__ = "App"


class App:
    def __init__(
        self, cache_enabled=False, upper_proxy: str = None, enable_https: bool = False
    ):
        self.cache_enabled = cache_enabled
        self.upper_proxy = upper_proxy
        self.enable_https = enable_https
        self.started = False

        self._lock = Lock()

    def start(self):
        if self._lock.acquire(blocking=False):
            if not self.started:
                logger.info("start ota http proxy app...")
                self.started = True
                self._ota_cache = ota_cache.OTACache(
                    upper_proxy=self.upper_proxy,
                    cache_enabled=self.cache_enabled,
                    init=True,
                    enable_https=self.enable_https,
                )
            self._lock.release()

    def stop(self):
        if self._lock.acquire(blocking=False):
            if self.started:
                logger.info("stopping ota http proxy app...")
                self._ota_cache.close()
                logger.info("shutdown server completed")
            self._lock.release()

    async def _respond_with_error(self, status: HTTPStatus, msg: str, send):
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
        if more:
            await send({"type": "http.response.body", "body": data, "more_body": True})
        else:
            await send({"type": "http.response.body", "body": b""})

    async def _init_response(self, status: HTTPStatus, headers: dict, send):
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": headers,
            }
        )

    async def _pull_data_and_send(self, url: str, send):
        f: ota_cache.OTAFile = await self._ota_cache.retrieve_file(url)

        # for any reason the response is not OK,
        # report to the client as 500
        if f is None:
            msg = f"proxy server failed to handle request {url=}"
            await self._respond_with_error(HTTPStatus.INTERNAL_SERVER_ERROR, msg, send)

            # terminate the request processing
            logger.error(f"failed to handle request {url=}")
            return

        # parse response
        # NOTE: currently only record content_type and content_encoding
        content_type = f.content_type
        content_encoding = f.content_encoding
        headers = []
        if content_type:
            headers.append([b"Content-Type", content_type.encode()])
        if content_encoding:
            headers.append([b"Content-Encoding", content_encoding.encode()])

        # prepare the response to the client
        await self._init_response(HTTPStatus.OK, headers, send)

        # stream the response to the client
        async for chunk in f:
            await self._send_chunk(chunk, True, send)
        # finish the streaming by send a 0 len payload
        await self._send_chunk(b"", False, send)

    async def app(self, scope, send):
        """
        the real entry for the server app
        """
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
        await self._pull_data_and_send(url, send)

    async def __call__(self, scope, receive, send):
        """
        the entrance of the asgi app
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
