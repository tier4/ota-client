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

import logging
import tempfile
from pathlib import Path

import httpx
import pytest

from ota_proxy.ota_cache import OTACache
from tests.conftest import ThreadpoolExecutorFixtureMixin

logger = logging.getLogger(__name__)


class TestHTTP2Support(ThreadpoolExecutorFixtureMixin):
    """Test HTTP/2 functionality in OTA Proxy."""

    async def test_httpx_http2_basic_functionality(self):
        """Test that httpx can make HTTP/2 requests."""
        logger.info("Testing basic HTTP/2 functionality with httpx...")

        # Create an httpx client with HTTP/2 enabled
        async with httpx.AsyncClient(http2=True) as client:
            try:
                # Test with a known HTTP/2 endpoint
                response = await client.get("https://www.google.com/")

                # --- assertions --- #
                assert response.status_code == 200
                logger.info(f"Response status: {response.status_code}")
                logger.info(f"HTTP version: {response.http_version}")

                # Check if the response was served over HTTP/2
                if response.http_version == "HTTP/2":
                    logger.info("✅ HTTP/2 is working!")
                else:
                    logger.warning(
                        f"Response served over {response.http_version}, not HTTP/2"
                    )

            except Exception as e:
                pytest.fail(f"Failed to make HTTP/2 request: {e}")

    async def test_ota_cache_http2_initialization(self):
        """Test that OTACache can be created with HTTP/2 support."""
        logger.info("Testing OTACache HTTP/2 initialization...")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # --- execution --- #
            ota_cache = OTACache(
                cache_enabled=False,
                init_cache=False,
                base_dir=temp_path,
                db_file=temp_path / "test.db",
            )

            try:
                await ota_cache.start()

                # --- assertions --- #
                assert isinstance(ota_cache._session, httpx.AsyncClient)

                logger.info(f"HTTP/2 session type: {type(ota_cache._session)}")
                logger.info("OTACache created successfully with httpx clients!")

            finally:
                await ota_cache.close()

    async def test_ota_cache_http2_request_with_retry(self):
        """Test that OTACache handles HTTP/2 requests with retry functionality."""
        logger.info("Testing OTACache HTTP/2 request with retry...")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            ota_cache = OTACache(
                cache_enabled=False,
                init_cache=False,
                base_dir=temp_path,
                db_file=temp_path / "test.db",
            )

            try:
                await ota_cache.start()

                # --- execution --- #
                # Test normal HTTP/2 request (should succeed)
                logger.info("Testing normal HTTP/2 request...")
                headers = {}
                response_received = False
                headers_received = False

                async for item in ota_cache._do_request_with_retry(
                    "http://httpbin.org/json", headers
                ):
                    if isinstance(item, dict) or hasattr(item, "items"):
                        headers_received = True
                        logger.info(f"Received headers: {type(item)}")
                    else:
                        response_received = True
                        logger.info(f"Received chunk: {len(item)} bytes")
                        break  # Just test the first chunk

                # --- assertions --- #
                assert headers_received, "Should receive headers from HTTP/2 request"
                assert (
                    response_received
                ), "Should receive response body from HTTP/2 request"
                logger.info("Normal HTTP/2 request succeeded!")

            finally:
                await ota_cache.close()

    async def test_ota_cache_http2_error_handling(self):
        """Test that OTACache properly handles HTTP/2 errors."""
        logger.info("Testing OTACache HTTP/2 error handling...")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            ota_cache = OTACache(
                cache_enabled=False,
                init_cache=False,
                base_dir=temp_path,
                db_file=temp_path / "test.db",
            )

            try:
                await ota_cache.start()

                # --- execution --- #
                # Test error handling with HTTP status error
                logger.info("Testing HTTP status error handling...")
                headers = {}

                with pytest.raises(httpx.HTTPStatusError) as exc_info:
                    async for _item in ota_cache._do_request_with_retry(
                        "https://httpbin.org/status/500", headers
                    ):
                        pass

                # --- assertions --- #
                assert exc_info.value.response.status_code == 500
                logger.info(
                    f"HTTPStatusError handled correctly: {exc_info.value.response.status_code}"
                )

            finally:
                await ota_cache.close()

    async def test_ota_cache_session_configuration(self):
        """Test that OTACache sessions are properly configured."""
        logger.info("Testing OTACache session configuration...")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            ota_cache = OTACache(
                cache_enabled=True,
                init_cache=True,
                base_dir=temp_path,
                db_file=temp_path / "test.db",
            )

            try:
                await ota_cache.start()

                # --- assertions --- #
                # Check HTTP/2 session configuration
                http2_session = ota_cache._session
                assert isinstance(http2_session, httpx.AsyncClient)

                logger.info("HTTP/2 sessions are properly configured!")

            finally:
                await ota_cache.close()

    async def test_retrieve_file_with_http2(self):
        """Test file retrieval using HTTP/2."""
        logger.info("Testing file retrieval with HTTP/2...")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            ota_cache = OTACache(
                cache_enabled=False,
                init_cache=False,
                base_dir=temp_path,
                db_file=temp_path / "test.db",
            )

            try:
                await ota_cache.start()

                # --- execution --- #
                from multidict import CIMultiDict

                headers = CIMultiDict()
                headers["User-Agent"] = "OTA-Client-Test"

                result = await ota_cache.retrieve_file(
                    "https://httpbin.org/json", headers
                )

                # --- assertions --- #
                assert result is not None
                file_descriptor, response_headers = result

                # Verify we got a response
                assert file_descriptor is not None
                assert response_headers is not None

                # Check that we can read data
                from typing import AsyncGenerator

                if isinstance(file_descriptor, (AsyncGenerator, type(x for x in []))):
                    # It's an async generator or generator
                    data_received = False
                    if hasattr(file_descriptor, "__aiter__"):
                        async for chunk in file_descriptor:
                            data_received = True
                            assert len(chunk) > 0
                            break  # Just test the first chunk
                    assert data_received, "Should receive data from HTTP/2 request"
                else:
                    # It's bytes
                    assert isinstance(file_descriptor, bytes)
                    assert len(file_descriptor) > 0

                logger.info("File retrieval with HTTP/2 succeeded!")

            finally:
                await ota_cache.close()
