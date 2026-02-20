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

"""Unit tests for ota_proxy.server_app (App ASGI application).

Test focus:
  1. Helper functions: parse_raw_headers, encode_headers
  2. App lifecycle: start / stop idempotency
  3. ASGI lifespan protocol compliance (uvicorn compatibility)
  4. HTTP handler routing logic
  5. Error handling for all exception types from ota_cache
  6. Data-streaming paths (_pull_data_and_send)
  7. uvicorn integration: verify the App works end-to-end with a real
     uvicorn.Server instance using our production configuration.

The unit tests (1-6) use a mocked OTACache so no real network or filesystem
access is required.  The integration tests (7) spin up a real uvicorn server
in-process and make actual HTTP requests through it.
"""

from __future__ import annotations

import asyncio
import socket
from http import HTTPStatus
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
import uvicorn
from multidict import CIMultiDict

from ota_proxy._consts import (
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
)
from ota_proxy.errors import (
    BaseOTACacheError,
    CacheProviderNotReady,
    CacheStreamingFailed,
    ReaderPoolBusy,
)
from ota_proxy.server_app import App, encode_headers, parse_raw_headers

# --------------------------------------------------------------------------- #
# ASGI test helpers
# --------------------------------------------------------------------------- #


def _make_http_scope(
    method: str = "GET",
    path: str = "http://example.com/data/file.bin",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> dict[str, Any]:
    """Build a minimal ASGI HTTP scope dict (as uvicorn would provide)."""
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers if headers is not None else [],
    }


def _make_lifespan_scope() -> dict[str, Any]:
    return {"type": "lifespan"}


class _MessageQueue:
    """Minimal ASGI receive / send channel backed by in-memory lists."""

    def __init__(self, recv_messages: list[dict]) -> None:
        self._recv = list(recv_messages)
        self._sent: list[dict] = []

    async def receive(self) -> dict:
        return self._recv.pop(0)

    async def send(self, msg: dict) -> None:
        self._sent.append(msg)

    @property
    def sent(self) -> list[dict]:
        return self._sent


def _make_ota_cache_mock() -> MagicMock:
    """Return a MagicMock that satisfies the OTACache interface."""
    mock = MagicMock()
    mock.start = AsyncMock()
    mock.close = AsyncMock()
    mock.retrieve_file = AsyncMock()
    return mock


# --------------------------------------------------------------------------- #
# Tests: parse_raw_headers
# --------------------------------------------------------------------------- #


class TestParseRawHeaders:
    """Tests for the parse_raw_headers() helper function."""

    def test_empty_headers(self):
        result = parse_raw_headers([])
        assert isinstance(result, CIMultiDict)
        assert len(result) == 0

    def test_authorization_header(self):
        raw = [(BHEADER_AUTHORIZATION, b"Bearer token123")]
        result = parse_raw_headers(raw)
        assert result[HEADER_AUTHORIZATION] == "Bearer token123"

    def test_cookie_header(self):
        raw = [(BHEADER_COOKIE, b"session=abc; user=xyz")]
        result = parse_raw_headers(raw)
        assert result[HEADER_COOKIE] == "session=abc; user=xyz"

    def test_ota_file_cache_control_header(self):
        raw = [(BHEADER_OTA_FILE_CACHE_CONTROL, b"no-cache")]
        result = parse_raw_headers(raw)
        assert result[HEADER_OTA_FILE_CACHE_CONTROL] == "no-cache"

    def test_unknown_headers_are_ignored(self):
        raw = [
            (b"x-custom-header", b"custom-value"),
            (b"accept", b"*/*"),
        ]
        result = parse_raw_headers(raw)
        assert len(result) == 0

    def test_malformed_tuple_is_skipped(self):
        """Headers with wrong tuple length must be silently ignored."""
        raw = [
            (b"only-key",),  # length 1
            (b"a", b"b", b"c"),  # length 3
            (BHEADER_AUTHORIZATION, b"valid"),
        ]
        result = parse_raw_headers(raw)  # type: ignore[arg-type]
        assert result[HEADER_AUTHORIZATION] == "valid"
        assert len(result) == 1

    def test_multiple_known_headers(self):
        raw = [
            (BHEADER_AUTHORIZATION, b"Basic xxx"),
            (BHEADER_COOKIE, b"k=v"),
            (BHEADER_OTA_FILE_CACHE_CONTROL, b"file-sha256=abc"),
        ]
        result = parse_raw_headers(raw)
        assert result[HEADER_AUTHORIZATION] == "Basic xxx"
        assert result[HEADER_COOKIE] == "k=v"
        assert result[HEADER_OTA_FILE_CACHE_CONTROL] == "file-sha256=abc"

    def test_leading_trailing_whitespace_is_stripped_for_auth_cookie(self):
        """Authorization and Cookie values are strip()ped per spec."""
        raw = [
            (BHEADER_AUTHORIZATION, b"  Bearer tok  "),
            (BHEADER_COOKIE, b"  session=x  "),
        ]
        result = parse_raw_headers(raw)
        assert result[HEADER_AUTHORIZATION] == "Bearer tok"
        assert result[HEADER_COOKIE] == "session=x"

    def test_unicode_surrogate_escape(self):
        """Bytes that are not valid UTF-8 should be handled via surrogateescape."""
        raw = [(BHEADER_AUTHORIZATION, b"Bearer \xff\xfe")]
        # Should not raise; result may contain surrogates
        result = parse_raw_headers(raw)
        assert HEADER_AUTHORIZATION in result


# --------------------------------------------------------------------------- #
# Tests: encode_headers
# --------------------------------------------------------------------------- #


class TestEncodeHeaders:
    """Tests for the encode_headers() helper function."""

    def test_empty_headers(self):
        result = encode_headers(CIMultiDict())
        assert result == []

    def test_content_encoding(self):
        headers: CIMultiDict[str] = CIMultiDict({HEADER_CONTENT_ENCODING: "gzip"})
        result = encode_headers(headers)
        assert (BHEADER_CONTENT_ENCODING, b"gzip") in result

    def test_content_type(self):
        headers: CIMultiDict[str] = CIMultiDict(
            {HEADER_CONTENT_TYPE: "application/octet-stream"}
        )
        result = encode_headers(headers)
        assert (BHEADER_CONTENT_TYPE, b"application/octet-stream") in result

    def test_ota_file_cache_control(self):
        headers: CIMultiDict[str] = CIMultiDict(
            {HEADER_OTA_FILE_CACHE_CONTROL: "file-sha256=deadbeef"}
        )
        result = encode_headers(headers)
        assert (BHEADER_OTA_FILE_CACHE_CONTROL, b"file-sha256=deadbeef") in result

    def test_unknown_header_not_included(self):
        """Headers not in the allow-list must not be forwarded to the client."""
        headers: CIMultiDict[str] = CIMultiDict(
            {
                HEADER_AUTHORIZATION: "Bearer tok",
                HEADER_COOKIE: "k=v",
                HEADER_CONTENT_TYPE: "text/plain",
            }
        )
        result = encode_headers(headers)
        keys = [k for k, _ in result]
        assert BHEADER_AUTHORIZATION not in keys
        assert BHEADER_COOKIE not in keys
        assert BHEADER_CONTENT_TYPE in keys

    def test_all_allowed_headers(self):
        headers: CIMultiDict[str] = CIMultiDict(
            {
                HEADER_CONTENT_ENCODING: "zstd",
                HEADER_CONTENT_TYPE: "application/octet-stream",
                HEADER_OTA_FILE_CACHE_CONTROL: "no-cache",
            }
        )
        result = encode_headers(headers)
        result_dict = dict(result)
        assert result_dict[BHEADER_CONTENT_ENCODING] == b"zstd"
        assert result_dict[BHEADER_CONTENT_TYPE] == b"application/octet-stream"
        assert result_dict[BHEADER_OTA_FILE_CACHE_CONTROL] == b"no-cache"

    def test_result_elements_are_tuples(self):
        """ASGI spec requires headers as list[tuple[bytes, bytes]]."""
        headers: CIMultiDict[str] = CIMultiDict({HEADER_CONTENT_TYPE: "text/plain"})
        result = encode_headers(headers)
        for item in result:
            assert isinstance(item, tuple)
            assert len(item) == 2
            assert isinstance(item[0], bytes)
            assert isinstance(item[1], bytes)


# --------------------------------------------------------------------------- #
# Tests: App lifecycle (start / stop)
# --------------------------------------------------------------------------- #


class TestAppLifecycle:
    """Tests for App.start() and App.stop() idempotency and state transitions."""

    async def test_start_calls_ota_cache_start(self):
        mock_cache = _make_ota_cache_mock()
        app = App(mock_cache)
        await app.start()
        mock_cache.start.assert_awaited_once()

    async def test_start_is_idempotent(self):
        """Calling start() twice must not call ota_cache.start() twice."""
        mock_cache = _make_ota_cache_mock()
        app = App(mock_cache)
        await app.start()
        await app.start()
        mock_cache.start.assert_awaited_once()

    async def test_stop_calls_ota_cache_close(self):
        mock_cache = _make_ota_cache_mock()
        app = App(mock_cache)
        await app.start()
        await app.stop()
        mock_cache.close.assert_awaited_once()

    async def test_stop_is_idempotent(self):
        """Calling stop() twice must not call ota_cache.close() twice."""
        mock_cache = _make_ota_cache_mock()
        app = App(mock_cache)
        await app.start()
        await app.stop()
        await app.stop()
        mock_cache.close.assert_awaited_once()

    async def test_stop_without_start_is_noop(self):
        """stop() on a never-started app must be a no-op."""
        mock_cache = _make_ota_cache_mock()
        app = App(mock_cache)
        await app.stop()
        mock_cache.close.assert_not_awaited()

    async def test_start_stop_start_cycle(self):
        """A full start → stop → start cycle should re-open the cache."""
        mock_cache = _make_ota_cache_mock()
        app = App(mock_cache)
        await app.start()
        await app.stop()
        await app.start()
        assert mock_cache.start.await_count == 2
        assert mock_cache.close.await_count == 1


# --------------------------------------------------------------------------- #
# Tests: ASGI lifespan protocol (uvicorn compatibility)
# --------------------------------------------------------------------------- #


class TestASGILifespanProtocol:
    """Verify that the App correctly handles the ASGI lifespan protocol."""

    async def test_startup_complete_message_sent(self):
        """App must send lifespan.startup.complete after receiving startup."""
        mock_cache = _make_ota_cache_mock()
        app = App(mock_cache)

        mq = _MessageQueue(
            [
                {"type": "lifespan.startup"},
                {"type": "lifespan.shutdown"},
            ]
        )
        await app(_make_lifespan_scope(), mq.receive, mq.send)

        sent_types = [m["type"] for m in mq.sent]
        assert "lifespan.startup.complete" in sent_types

    async def test_shutdown_complete_message_sent(self):
        """App must send lifespan.shutdown.complete after receiving shutdown."""
        mock_cache = _make_ota_cache_mock()
        app = App(mock_cache)

        mq = _MessageQueue(
            [
                {"type": "lifespan.startup"},
                {"type": "lifespan.shutdown"},
            ]
        )
        await app(_make_lifespan_scope(), mq.receive, mq.send)

        sent_types = [m["type"] for m in mq.sent]
        assert "lifespan.shutdown.complete" in sent_types

    async def test_full_lifespan_sequence(self):
        """startup → startup.complete → shutdown → shutdown.complete."""
        mock_cache = _make_ota_cache_mock()
        app = App(mock_cache)

        mq = _MessageQueue(
            [
                {"type": "lifespan.startup"},
                {"type": "lifespan.shutdown"},
            ]
        )
        await app(_make_lifespan_scope(), mq.receive, mq.send)

        assert mq.sent[0]["type"] == "lifespan.startup.complete"
        assert mq.sent[1]["type"] == "lifespan.shutdown.complete"

    async def test_lifespan_calls_start_and_stop(self):
        """Lifespan protocol must invoke App.start() and App.stop()."""
        mock_cache = _make_ota_cache_mock()
        app = App(mock_cache)

        mq = _MessageQueue(
            [
                {"type": "lifespan.startup"},
                {"type": "lifespan.shutdown"},
            ]
        )
        await app(_make_lifespan_scope(), mq.receive, mq.send)

        mock_cache.start.assert_awaited_once()
        mock_cache.close.assert_awaited_once()

    async def test_unknown_scope_type_is_silently_ignored(self):
        """Unknown scope types must be ignored without error."""
        mock_cache = _make_ota_cache_mock()
        app = App(mock_cache)

        scope = {"type": "websocket"}
        mq = _MessageQueue([])
        # Must complete without raising
        await app(scope, mq.receive, mq.send)
        assert mq.sent == []


# --------------------------------------------------------------------------- #
# Tests: HTTP handler routing
# --------------------------------------------------------------------------- #


class TestHTTPHandlerRouting:
    """Tests for App.http_handler() routing decisions."""

    async def _call_http(
        self,
        app: App,
        *,
        method: str = "GET",
        path: str = "http://example.com/data/file.bin",
        headers: list[tuple[bytes, bytes]] | None = None,
    ) -> list[dict]:
        """Helper: call the app as an HTTP request and return sent messages."""
        scope = _make_http_scope(method=method, path=path, headers=headers)
        mq = _MessageQueue([])
        await app(scope, mq.receive, mq.send)
        return mq.sent

    async def test_non_get_method_returns_400(self):
        mock_cache = _make_ota_cache_mock()
        app = App(mock_cache)
        sent = await self._call_http(app, method="POST")

        assert sent[0]["type"] == "http.response.start"
        assert sent[0]["status"] == HTTPStatus.BAD_REQUEST

    async def test_invalid_url_no_scheme_returns_400(self):
        mock_cache = _make_ota_cache_mock()
        app = App(mock_cache)
        # A relative path has no scheme
        sent = await self._call_http(app, path="/relative/path")

        assert sent[0]["type"] == "http.response.start"
        assert sent[0]["status"] == HTTPStatus.BAD_REQUEST

    async def test_valid_get_delegates_to_pull_data(self):
        """A valid GET with a proper URL must call retrieve_file."""
        mock_cache = _make_ota_cache_mock()
        mock_cache.retrieve_file.return_value = (
            b"file content",
            CIMultiDict({HEADER_CONTENT_TYPE: "application/octet-stream"}),
        )
        app = App(mock_cache)
        sent = await self._call_http(app, path="http://example.com/data/file.bin")

        mock_cache.retrieve_file.assert_awaited_once()
        assert sent[0]["type"] == "http.response.start"
        assert sent[0]["status"] == HTTPStatus.OK

    async def test_semaphore_full_returns_429(self):
        """When all semaphore slots are occupied, requests get 429."""
        mock_cache = _make_ota_cache_mock()
        # Use max_concurrent_requests=1 for easy saturation
        app = App(mock_cache, max_concurrent_requests=1)

        # Saturate the semaphore manually
        await app._se.acquire()
        try:
            sent = await self._call_http(app, path="http://example.com/data/file.bin")
            assert sent[0]["type"] == "http.response.start"
            assert sent[0]["status"] == HTTPStatus.TOO_MANY_REQUESTS
        finally:
            app._se.release()


# --------------------------------------------------------------------------- #
# Tests: _pull_data_and_send – data streaming paths
# --------------------------------------------------------------------------- #


class TestPullDataAndSend:
    """Tests for App._pull_data_and_send(), covering all streaming paths."""

    async def _do_pull(
        self,
        app: App,
        *,
        path: str = "http://example.com/data/file.bin",
        headers: list[tuple[bytes, bytes]] | None = None,
    ) -> list[dict]:
        scope = _make_http_scope(path=path, headers=headers)
        mq = _MessageQueue([])
        await app._pull_data_and_send(path, scope, mq.send)
        return mq.sent

    async def test_bytes_response_sends_ok_and_body(self):
        content = b"hello world"
        mock_cache = _make_ota_cache_mock()
        mock_cache.retrieve_file.return_value = (
            content,
            CIMultiDict({HEADER_CONTENT_TYPE: "text/plain"}),
        )
        app = App(mock_cache)
        sent = await self._do_pull(app)

        assert sent[0]["type"] == "http.response.start"
        assert sent[0]["status"] == HTTPStatus.OK
        assert sent[1]["type"] == "http.response.body"
        assert sent[1]["body"] == content

    async def test_async_generator_streams_chunks(self):
        """An async-generator file descriptor must be streamed as multiple chunks."""
        chunks = [b"chunk1", b"chunk2", b"chunk3"]

        async def _async_gen():
            for c in chunks:
                yield c

        mock_cache = _make_ota_cache_mock()
        mock_cache.retrieve_file.return_value = (
            _async_gen(),
            CIMultiDict({HEADER_CONTENT_TYPE: "application/octet-stream"}),
        )
        app = App(mock_cache)
        sent = await self._do_pull(app)

        # First message: response start
        assert sent[0]["type"] == "http.response.start"
        assert sent[0]["status"] == HTTPStatus.OK

        # Intermediate messages: streaming body chunks with more_body=True
        body_chunks = [m for m in sent[1:] if m["type"] == "http.response.body"]
        streaming_bodies = [m["body"] for m in body_chunks if m.get("more_body")]
        assert streaming_bodies == chunks

        # Last message: empty body to terminate stream
        assert sent[-1]["body"] == b""
        assert not sent[-1].get("more_body")

    async def test_retrieve_none_sends_500(self):
        """retrieve_file returning None must result in 500."""
        mock_cache = _make_ota_cache_mock()
        mock_cache.retrieve_file.return_value = None
        app = App(mock_cache)
        sent = await self._do_pull(app)

        assert sent[0]["status"] == HTTPStatus.INTERNAL_SERVER_ERROR

    async def test_exception_during_streaming_sends_empty_chunk(self):
        """An exception raised mid-stream must terminate by sending an empty body."""

        async def _failing_gen():
            yield b"partial"
            raise CacheStreamingFailed("stream failed")

        mock_cache = _make_ota_cache_mock()
        mock_cache.retrieve_file.return_value = (
            _failing_gen(),
            CIMultiDict(),
        )
        app = App(mock_cache)
        sent = await self._do_pull(app)

        # Response start must have been sent before the failure
        assert sent[0]["type"] == "http.response.start"
        # Final message must be an empty body terminator
        assert sent[-1]["type"] == "http.response.body"
        assert sent[-1]["body"] == b""


# --------------------------------------------------------------------------- #
# Tests: _error_handling_for_cache_retrieving
# --------------------------------------------------------------------------- #


class TestErrorHandlingForCacheRetrieving:
    """Verify that each exception type maps to the correct HTTP status code."""

    async def _get_response_status(self, exc: Exception) -> int:
        mock_cache = _make_ota_cache_mock()
        app = App(mock_cache)
        sent: list[dict] = []

        async def _send(msg: dict) -> None:
            sent.append(msg)

        await app._error_handling_for_cache_retrieving(exc, "http://x.com/f", _send)
        return sent[0]["status"]

    async def test_reader_pool_busy_returns_503(self):
        status = await self._get_response_status(ReaderPoolBusy("busy"))
        assert status == HTTPStatus.SERVICE_UNAVAILABLE

    async def test_cache_provider_not_ready_returns_503(self):
        status = await self._get_response_status(CacheProviderNotReady("not ready"))
        assert status == HTTPStatus.SERVICE_UNAVAILABLE

    async def test_aiohttp_client_response_error_passthrough(self):
        """4xx/5xx from upstream should be passed through unchanged."""
        exc = aiohttp.ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=HTTPStatus.NOT_FOUND,
        )
        status = await self._get_response_status(exc)
        assert status == HTTPStatus.NOT_FOUND

    async def test_aiohttp_client_connection_error_returns_502(self):
        exc = aiohttp.ClientConnectionError("conn err")
        status = await self._get_response_status(exc)
        assert status == HTTPStatus.BAD_GATEWAY

    async def test_aiohttp_client_error_returns_503(self):
        exc = aiohttp.ClientError("client err")
        status = await self._get_response_status(exc)
        assert status == HTTPStatus.SERVICE_UNAVAILABLE

    async def test_base_ota_cache_error_returns_500(self):
        exc = BaseOTACacheError("cache error")
        status = await self._get_response_status(exc)
        assert status == HTTPStatus.INTERNAL_SERVER_ERROR

    async def test_stop_async_iteration_returns_500(self):
        status = await self._get_response_status(StopAsyncIteration())
        assert status == HTTPStatus.INTERNAL_SERVER_ERROR

    async def test_unhandled_exception_returns_500(self):
        status = await self._get_response_status(RuntimeError("unexpected"))
        assert status == HTTPStatus.INTERNAL_SERVER_ERROR


# --------------------------------------------------------------------------- #
# Tests: _error_handling_during_transferring
# --------------------------------------------------------------------------- #


class TestErrorHandlingDuringTransferring:
    """All mid-transfer exceptions must terminate with an empty body chunk."""

    async def _get_last_body(self, exc: Exception) -> bytes:
        mock_cache = _make_ota_cache_mock()
        app = App(mock_cache)
        sent: list[dict] = []

        async def _send(msg: dict) -> None:
            sent.append(msg)

        await app._error_handling_during_transferring(exc, "http://x.com/f", _send)
        return sent[-1]["body"]

    async def test_base_ota_cache_error_sends_empty_chunk(self):
        body = await self._get_last_body(CacheStreamingFailed("fail"))
        assert body == b""

    async def test_stop_async_iteration_sends_empty_chunk(self):
        body = await self._get_last_body(StopAsyncIteration())
        assert body == b""

    async def test_unhandled_exception_sends_empty_chunk(self):
        body = await self._get_last_body(RuntimeError("unexpected"))
        assert body == b""


# --------------------------------------------------------------------------- #
# Tests: ASGI response format compliance (uvicorn compatibility)
# --------------------------------------------------------------------------- #


class TestASGIResponseFormatCompliance:
    """Verify that the App emits ASGI messages in the format uvicorn expects.

    These tests guard against regressions introduced by uvicorn version upgrades
    that may tighten validation of the ASGI response contract.
    """

    async def _collect_http_messages(
        self,
        mock_cache: MagicMock,
        *,
        method: str = "GET",
        path: str = "http://example.com/data/file.bin",
    ) -> list[dict]:
        app = App(mock_cache)
        scope = _make_http_scope(method=method, path=path)
        mq = _MessageQueue([])
        await app(scope, mq.receive, mq.send)
        return mq.sent

    async def test_response_start_has_required_fields(self):
        """http.response.start must carry 'type', 'status', and 'headers'."""
        mock_cache = _make_ota_cache_mock()
        mock_cache.retrieve_file.return_value = (b"data", CIMultiDict())
        sent = await self._collect_http_messages(mock_cache)

        start = sent[0]
        assert start["type"] == "http.response.start"
        assert isinstance(start["status"], int)
        assert "headers" in start
        assert isinstance(start["headers"], list)

    async def test_response_headers_are_bytes_tuples(self):
        """Each header in http.response.start must be a tuple[bytes, bytes]."""
        mock_cache = _make_ota_cache_mock()
        mock_cache.retrieve_file.return_value = (
            b"data",
            CIMultiDict(
                {
                    HEADER_CONTENT_TYPE: "application/octet-stream",
                    HEADER_CONTENT_ENCODING: "identity",
                }
            ),
        )
        sent = await self._collect_http_messages(mock_cache)

        for header in sent[0]["headers"]:
            assert isinstance(header, tuple), f"Expected tuple, got {type(header)}"
            assert len(header) == 2
            key, val = header
            assert isinstance(key, bytes)
            assert isinstance(val, bytes)

    async def test_response_body_has_required_fields(self):
        """http.response.body must carry 'type' and 'body'."""
        mock_cache = _make_ota_cache_mock()
        mock_cache.retrieve_file.return_value = (b"content", CIMultiDict())
        sent = await self._collect_http_messages(mock_cache)

        body_msg = sent[1]
        assert body_msg["type"] == "http.response.body"
        assert isinstance(body_msg["body"], bytes)

    async def test_streaming_body_has_more_body_flag(self):
        """Intermediate streaming chunks must have more_body=True."""

        async def _gen():
            yield b"chunk"

        mock_cache = _make_ota_cache_mock()
        mock_cache.retrieve_file.return_value = (_gen(), CIMultiDict())
        sent = await self._collect_http_messages(mock_cache)

        # The first body message (streaming chunk) must have more_body=True
        first_body = next(
            m for m in sent if m.get("type") == "http.response.body" and m.get("body")
        )
        assert first_body.get("more_body") is True

    async def test_error_response_status_is_integer(self):
        """Error responses must use integer HTTP status codes (not HTTPStatus enum)."""
        mock_cache = _make_ota_cache_mock()
        # Use non-GET to trigger 400
        sent = await self._collect_http_messages(mock_cache, method="DELETE")

        status = sent[0]["status"]
        # HTTPStatus is a subclass of int, so int(status) == status, but
        # uvicorn >=0.30 accepts both.  Guard against accidental string values.
        assert isinstance(status, int)

    async def test_only_one_response_start_per_request(self):
        """A single HTTP request must result in exactly one http.response.start."""
        mock_cache = _make_ota_cache_mock()
        mock_cache.retrieve_file.return_value = (b"ok", CIMultiDict())
        sent = await self._collect_http_messages(mock_cache)

        starts = [m for m in sent if m.get("type") == "http.response.start"]
        assert len(starts) == 1


# --------------------------------------------------------------------------- #
# Tests: uvicorn integration (version-upgrade compatibility)
# --------------------------------------------------------------------------- #


def _find_free_port() -> int:
    """Return an available TCP port on localhost."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestUvicornCompatibility:
    """End-to-end tests that run a real uvicorn.Server with our App.

    These tests validate that our production uvicorn usage does not break
    across version upgrades.  Key configuration under test:

        uvicorn.Config(app, lifespan="on", loop="uvloop", http="h11")
        uvicorn.Server(config).serve()   (awaited as a coroutine)

    Requests are sent via aiohttp using the HTTP proxy protocol
    (absolute-form URI target), which is exactly how otaclient uses otaproxy.
    """

    # --- fixture ------------------------------------------------------------ #

    @pytest.fixture
    async def live_server(self):
        """Start uvicorn in-process and yield (server, port, mock_cache).

        loop="none" keeps uvicorn on the current event loop so that the fixture
        can co-exist with pytest-asyncio's event loop.  The lifespan="on" and
        http="h11" settings mirror the production configuration in __init__.py.
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
            http="h11",  # required for HTTP proxy (absolute-form URI)
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

    # --- config-only tests (no server needed) ------------------------------- #

    def test_production_config_params_are_accepted(self):
        """uvicorn.Config must not raise with our production parameter set.

        This test validates that the uvicorn version installed in the project
        still accepts loop="uvloop", http="h11", and lifespan="on".
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
            http="h11",
        )
        assert config.lifespan == "on"
        assert config.http == "h11"

    # --- lifespan integration tests ---------------------------------------- #

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

    async def test_proxy_get_returns_200_and_body(self, live_server):
        """A proxy-style GET must return 200 and the file content."""
        _server, port, _mock_cache, content = live_server
        proxy_url = f"http://127.0.0.1:{port}"
        target_url = "http://ota-image.example.com/data/firmware.bin"

        async with aiohttp.ClientSession() as session:
            async with session.get(target_url, proxy=proxy_url) as resp:
                assert resp.status == HTTPStatus.OK
                body = await resp.read()

        assert body == content

    async def test_proxy_get_forwards_request_url_to_ota_cache(self, live_server):
        """The URL seen by OTACache.retrieve_file must match the original target."""
        _server, port, mock_cache, _content = live_server
        proxy_url = f"http://127.0.0.1:{port}"
        target_url = "http://ota-image.example.com/data/firmware.bin"

        async with aiohttp.ClientSession() as session:
            async with session.get(target_url, proxy=proxy_url) as resp:
                assert resp.status == HTTPStatus.OK

        called_url = mock_cache.retrieve_file.call_args[0][0]
        assert called_url == target_url

    async def test_proxy_non_get_returns_400(self, live_server):
        """A non-GET request through the proxy must be rejected with 400."""
        _server, port, _mock_cache, _content = live_server
        proxy_url = f"http://127.0.0.1:{port}"
        target_url = "http://ota-image.example.com/data/firmware.bin"

        async with aiohttp.ClientSession() as session:
            async with session.post(target_url, proxy=proxy_url) as resp:
                assert resp.status == HTTPStatus.BAD_REQUEST

    async def test_content_type_header_forwarded_to_client(self, live_server):
        """The Content-Type header returned by OTACache must reach the client."""
        _server, port, _mock_cache, _content = live_server
        proxy_url = f"http://127.0.0.1:{port}"
        target_url = "http://ota-image.example.com/data/firmware.bin"

        async with aiohttp.ClientSession() as session:
            async with session.get(target_url, proxy=proxy_url) as resp:
                assert resp.status == HTTPStatus.OK
                assert "application/octet-stream" in resp.content_type
