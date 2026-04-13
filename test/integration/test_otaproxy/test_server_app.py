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
"""Integration tests for ota_proxy.server_app with uvicorn.

These tests spin up a real uvicorn.Server in-process (with httptools as the
HTTP parser, matching production configuration) and make actual HTTP requests
through it.  A mocked OTACache is used so no real network or filesystem
access to the upstream OTA server is required.

Key configuration under test:

    uvicorn.Config(app, lifespan="on", loop="uvloop", http="httptools")
    uvicorn.Server(config).serve()   (awaited as a coroutine)

httptools strips absolute-form URIs to just the path, so the App
reconstructs the full URL from the Host header.
"""

from __future__ import annotations

import asyncio
import socket
from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
import uvicorn
from multidict import CIMultiDict

from ota_proxy._consts import HEADER_CONTENT_TYPE
from ota_proxy.server_app import App


def _make_ota_cache_mock() -> MagicMock:
    """Return a MagicMock that satisfies the OTACache interface."""
    mock = MagicMock()
    mock.start = AsyncMock()
    mock.close = AsyncMock()
    mock.retrieve_file = AsyncMock()
    return mock


def _find_free_port() -> int:
    """Return an available TCP port on localhost."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestUvicornCompatibility:
    """End-to-end tests that run a real uvicorn.Server with our App.

    These tests validate that our production uvicorn usage does not break
    across version upgrades.
    """

    @pytest.fixture
    async def live_server(self):
        """Start uvicorn in-process and yield (server, port, mock_cache, content).

        loop="none" keeps uvicorn on the current event loop so that the fixture
        can co-exist with pytest-asyncio's event loop.  The lifespan="on" and
        http="httptools" settings mirror the production configuration in
        __init__.py.
        """
        content = b"test-payload"
        mock_cache = _make_ota_cache_mock()
        mock_cache.retrieve_file.return_value = (
            content,
            CIMultiDict({HEADER_CONTENT_TYPE: "application/octet-stream"}),
        )
        app = App(mock_cache)
        port = _find_free_port()

        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            lifespan="on",
            loop="none",  # reuse pytest-asyncio's event loop
            http="httptools",
        )
        server = uvicorn.Server(config)
        serve_task = asyncio.create_task(server.serve())

        # Poll until uvicorn signals it has finished startup
        for _ in range(50):
            if server.started:
                break
            await asyncio.sleep(0.1)
        else:
            server.should_exit = True
            await serve_task
            pytest.fail("uvicorn did not start within 5 s")

        try:
            yield server, port, mock_cache, content
        finally:
            server.should_exit = True
            await serve_task

    def test_production_config_params_are_accepted(self):
        """uvicorn.Config must not raise with our production parameter set.

        This test validates that the uvicorn version installed in the project
        still accepts loop="uvloop", http="httptools", and lifespan="on".
        """
        mock_cache = _make_ota_cache_mock()
        app = App(mock_cache)
        # Must not raise regardless of uvicorn version
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=_find_free_port(),
            log_level="error",
            lifespan="on",
            loop="uvloop",
            http="httptools",
        )
        assert config.lifespan == "on"
        assert config.http == "httptools"

    async def test_lifespan_startup_triggers_app_start(self, live_server):
        """uvicorn's lifespan handling must call App.start() on startup."""
        _server, _port, mock_cache, _content = live_server
        mock_cache.start.assert_awaited_once()

    async def test_lifespan_shutdown_triggers_app_stop(self, live_server):
        """uvicorn's lifespan handling must call App.stop() on shutdown."""
        server, _port, mock_cache, _content = live_server
        # Trigger shutdown by setting should_exit; the fixture teardown awaits it
        server.should_exit = True
        # Give the server a moment to process the shutdown
        for _ in range(30):
            if not server.started:
                break
            await asyncio.sleep(0.1)
        mock_cache.close.assert_awaited_once()

    # --- HTTP request integration tests ------------------------------------ #

    async def test_get_returns_200_and_body(self, live_server):
        """A GET with a remote Host header must return 200 and the file content.

        httptools strips absolute URIs, so the App reconstructs the URL from
        the Host header.  We send the request directly to the server with a
        Host header set to the target origin.
        """
        _server, port, _mock_cache, content = live_server
        url = f"http://127.0.0.1:{port}/data/firmware.bin"
        headers = {"Host": "ota-image.example.com"}

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                assert resp.status == HTTPStatus.OK
                body = await resp.read()

        assert body == content

    async def test_get_forwards_reconstructed_url_to_ota_cache(self, live_server):
        """The URL seen by OTACache.retrieve_file must be reconstructed from Host + path."""
        _server, port, mock_cache, _content = live_server
        url = f"http://127.0.0.1:{port}/data/firmware.bin"
        headers = {"Host": "ota-image.example.com"}

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                assert resp.status == HTTPStatus.OK

        called_url = mock_cache.retrieve_file.call_args[0][0]
        assert called_url == "http://ota-image.example.com/data/firmware.bin"

    async def test_non_get_returns_400(self, live_server):
        """A non-GET request must be rejected with 400."""
        _server, port, _mock_cache, _content = live_server
        url = f"http://127.0.0.1:{port}/data/firmware.bin"
        headers = {"Host": "ota-image.example.com"}

        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers) as resp:
                assert resp.status == HTTPStatus.BAD_REQUEST

    async def test_content_type_header_forwarded_to_client(self, live_server):
        """The Content-Type header returned by OTACache must reach the client."""
        _server, port, _mock_cache, _content = live_server
        url = f"http://127.0.0.1:{port}/data/firmware.bin"
        headers = {"Host": "ota-image.example.com"}

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                assert resp.status == HTTPStatus.OK
                assert "application/octet-stream" in resp.content_type
