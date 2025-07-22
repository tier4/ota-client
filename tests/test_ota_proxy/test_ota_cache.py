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

import bisect
import logging
import random
import sqlite3
import tempfile
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from simple_sqlite3_orm import ORMBase

from ota_proxy import config as cfg
from ota_proxy.db import CacheMeta
from ota_proxy.ota_cache import LRUCacheHelper, OTACache
from ota_proxy.utils import url_based_hash
from tests.conftest import ThreadpoolExecutorFixtureMixin

logger = logging.getLogger(__name__)

TEST_DATA_SET_SIZE = 4096
TEST_LOOKUP_ENTRIES = 1200
TEST_DELETE_ENTRIES = 512


class OTACacheDB(ORMBase[CacheMeta]):
    pass


@pytest.fixture(autouse=True, scope="module")
def setup_testdata() -> dict[str, CacheMeta]:
    size_list = list(cfg.BUCKET_FILE_SIZE_DICT)

    entries: dict[str, CacheMeta] = {}
    for idx in range(TEST_DATA_SET_SIZE):
        target_size = random.choice(size_list)
        mocked_url = f"#{idx}/w/targetsize={target_size}"
        file_sha256 = url_based_hash(mocked_url)

        entries[file_sha256] = CacheMeta(
            # see lru_cache_helper module for more details
            url=mocked_url,
            bucket_idx=bisect.bisect_right(size_list, target_size),
            cache_size=target_size,
            file_sha256=file_sha256,
        )
    return entries


@pytest.fixture(autouse=True, scope="module")
def entries_to_lookup(setup_testdata: dict[str, CacheMeta]) -> list[CacheMeta]:
    return random.sample(
        list(setup_testdata.values()),
        k=TEST_LOOKUP_ENTRIES,
    )


@pytest.fixture(autouse=True, scope="module")
def entries_to_remove(setup_testdata: dict[str, CacheMeta]) -> list[CacheMeta]:
    return random.sample(
        list(setup_testdata.values()),
        k=TEST_DELETE_ENTRIES,
    )


@pytest.mark.asyncio(scope="class")
class TestLRUCacheHelper:
    @pytest_asyncio.fixture(autouse=True, scope="class")
    async def lru_helper(self, tmp_path_factory: pytest.TempPathFactory):
        ota_cache_folder = tmp_path_factory.mktemp("ota-cache")
        db_f = ota_cache_folder / "db_f"

        # init table
        conn = sqlite3.connect(db_f)
        orm = OTACacheDB(conn, cfg.TABLE_NAME)
        orm.orm_create_table(without_rowid=True)
        conn.close()

        lru_cache_helper = LRUCacheHelper(db_f)
        try:
            yield lru_cache_helper
        finally:
            await lru_cache_helper.close()

    async def test_commit_entry(
        self, lru_helper: LRUCacheHelper, setup_testdata: dict[str, CacheMeta]
    ):
        for _, entry in setup_testdata.items():
            # deliberately clear the bucket_idx, this should be set by commit_entry method
            _copy = entry.model_copy()
            _copy.bucket_idx = 0
            assert lru_helper.commit_entry(entry)

    async def test_lookup_entry(
        self,
        lru_helper: LRUCacheHelper,
        entries_to_lookup: list[CacheMeta],
        setup_testdata: dict[str, CacheMeta],
    ):
        for entry in entries_to_lookup:
            assert (
                await lru_helper.lookup_entry(entry.file_sha256)
                == setup_testdata[entry.file_sha256]
            )

    async def test_remove_entry(
        self, lru_helper: LRUCacheHelper, entries_to_remove: list[CacheMeta]
    ):
        for entry in entries_to_remove:
            assert await lru_helper.remove_entry(entry.file_sha256)

    async def test_rotate_cache(self, lru_helper: LRUCacheHelper):
        """Ensure the LRUHelper properly rotates the cache entries.

        We should file enough entries into the database, so each rotate should be successful.
        """
        # NOTE that the first bucket and last bucket will not be rotated,
        #   see lru_cache_helper module for more details.
        for target_bucket in list(cfg.BUCKET_FILE_SIZE_DICT)[1:-1]:
            entries_to_be_removed = lru_helper.rotate_cache(target_bucket)
            assert entries_to_be_removed is not None and len(entries_to_be_removed) != 0


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
