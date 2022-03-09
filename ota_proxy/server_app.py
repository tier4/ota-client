from http import HTTPStatus
from threading import Lock
from typing import Dict, List

import aiohttp

from . import ota_cache
from .config import config as cfg

import logging

logger = logging.getLogger(__name__)
logger.setLevel(cfg.LOG_LEVEL)

# only expose app
__all__ = "App"


class App:
    def __init__(
        self,
        *,
        upper_proxy: str = None,
        cache_enabled=False,
        enable_https=False,
        init_cache=True,
    ):
        self.cache_enabled = cache_enabled
        self.upper_proxy = upper_proxy
        self.enable_https = enable_https
        self.init_cache = init_cache
        logger.debug(
            f"init ota-proxy({cache_enabled=}, {init_cache=}, {enable_https=}, {upper_proxy=})"
        )

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
                    init=self.init_cache,
                    enable_https=self.enable_https,
                )
            self._lock.release()

    def stop(self):
        if self._lock.acquire(blocking=False):
            if self.started:
                logger.info("stopping ota http proxy app...")
                self._ota_cache.close()
                logger.info("shutdown server completed")
                self.started = False
            self._lock.release()

    @staticmethod
    def parse_raw_cookies(cookies_bytes: bytes) -> Dict[str, str]:
        """
        parse raw cookies bytes into dict
        """
        cookie_pairs: List[str] = cookies_bytes.decode().split(";")
        res = dict()
        for p in cookie_pairs:
            k, v = p.strip().split("=")
            res[k] = v

        return res

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

    async def _pull_data_and_send(self, url: str, scope, send):
        # pass cookies and other headers needed for proxy into the ota_cache module
        cookies_dict: Dict[str, str] = dict()
        extra_headers: Dict[str, str] = dict()
        # currently we only need cookie and/or authorization header
        for header in scope["headers"]:
            if header[0] == b"cookie":
                cookies_dict = self.parse_raw_cookies(header[1])
            elif header[0] == b"authorization":
                extra_headers["Authorization"] = header[1].decode()

        f: ota_cache.OTAFile = None
        try:
            f = await self._ota_cache.retrieve_file(url, cookies_dict, extra_headers)
        except aiohttp.ClientResponseError as e:
            await self._respond_with_error(e.status, e.message, send)
            return
        except aiohttp.ClientConnectionError:
            await self._respond_with_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "failed to connect to remote server",
                send,
            )
            return
        except aiohttp.ClientError as e:
            await self._respond_with_error(
                HTTPStatus.INTERNAL_SERVER_ERROR, f"client error: {e!r}", send
            )
            return
        except Exception as e:
            await self._respond_with_error(
                HTTPStatus.INTERNAL_SERVER_ERROR, f"{e!r}", send
            )
            return

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
        await self._pull_data_and_send(url, scope, send)

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
