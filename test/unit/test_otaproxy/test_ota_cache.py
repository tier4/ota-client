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
import random
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from multidict import CIMultiDict, CIMultiDictProxy

from ota_proxy import config as cfg
from ota_proxy.cache_index import CacheIndex
from ota_proxy.db import CacheMeta
from ota_proxy.ota_cache import OTACache
from ota_proxy.utils import url_based_hash

logger = logging.getLogger(__name__)

TEST_DATA_SET_SIZE = 4096
TEST_LOOKUP_ENTRIES = 1200
TEST_DELETE_ENTRIES = 512


def _generate_test_entries() -> dict[str, CacheMeta]:
    """Generate a set of CacheMeta entries with various cache sizes."""
    size_list = [0, 1024, 2048, 4096, 8192, 16384, 32768, 256 * 1024, 1024**2]

    entries: dict[str, CacheMeta] = {}
    for idx in range(TEST_DATA_SET_SIZE):
        target_size = random.choice(size_list)
        mocked_url = f"#{idx}/w/targetsize={target_size}"
        file_sha256 = url_based_hash(mocked_url)

        entries[file_sha256] = CacheMeta(
            url=mocked_url,
            cache_size=target_size,
            file_sha256=file_sha256,
        )
    return entries


#
# ------------ Tests: CacheIndex ------------ #
#


class TestCacheIndex:
    """Tests for CacheIndex commit, lookup, and remove operations."""

    @pytest.fixture(scope="class")
    def test_entries(self) -> dict[str, CacheMeta]:
        return _generate_test_entries()

    @pytest.fixture(scope="class")
    def cache_index(self, tmp_path_factory: pytest.TempPathFactory):
        ota_cache_folder = tmp_path_factory.mktemp("cache_index") / "ota-cache"
        ota_cache_folder.mkdir()
        db_f = ota_cache_folder / "db_f"

        # Create cache files so preload doesn't discard entries
        # (preload checks os.path.exists for each entry)
        cache_index = CacheIndex(db_f, ota_cache_folder, init_db=True)
        try:
            yield cache_index, ota_cache_folder
        finally:
            cache_index.close()

    def test_commit_entry(
        self,
        cache_index: tuple[CacheIndex, Path],
        test_entries: dict[str, CacheMeta],
    ):
        idx, base_dir = cache_index
        for entry in test_entries.values():
            # Create a placeholder cache file so the entry is valid on reload
            (base_dir / entry.file_sha256).touch()
            assert idx.commit_entry(entry)

    def test_lookup_entry(
        self,
        cache_index: tuple[CacheIndex, Path],
        test_entries: dict[str, CacheMeta],
    ):
        idx, _ = cache_index
        # Confirm all entries are added
        for entry in test_entries.values():
            result = idx.lookup_entry(entry.file_sha256)
            assert result is not None
            assert result.cache_size == test_entries[entry.file_sha256].cache_size

    def test_remove_entry(
        self,
        cache_index: tuple[CacheIndex, Path],
        test_entries: dict[str, CacheMeta],
    ):
        idx, _ = cache_index

        # Remove a sample and verify they are gone
        entries_to_remove = random.sample(
            list(test_entries.values()), k=TEST_DELETE_ENTRIES
        )
        for entry in entries_to_remove:
            idx.remove_entry(entry.file_sha256)
            assert idx.lookup_entry(entry.file_sha256) is None


#
# ------------ Tests: OTACache CDN cache-hit detection ------------ #
#


class TestOTACacheCDNCacheHit:
    """Tests for CDN cache-hit detection via X-Cache response header."""

    @pytest.mark.parametrize(
        "x_cache_value, expected_hits",
        [
            ("Hit from CloudFront", 1),
            ("hit from cloudfront", 1),
            ("Miss from CloudFront", 0),
            ("miss from cloudfront", 0),
        ],
    )
    async def test_retrieve_file_by_downloading_cdn_cache(
        self, x_cache_value: str, expected_hits: int
    ):
        cache = OTACache(
            cache_enabled=False,
            init_cache=False,
            shm_metrics_writer=None,
        )

        async def mock_do_request(raw_url, headers):
            yield CIMultiDictProxy(CIMultiDict({"X-Cache": x_cache_value}))
            yield b"data"

        cache._do_request = MagicMock(side_effect=mock_do_request)

        remote_fd, _ = await cache._retrieve_file_by_downloading("dummy_url", {})
        [chunk async for chunk in remote_fd]  # consume the generator
        assert cache._metrics_data.cache_cdn_hits == expected_hits


#
# ------------ Tests: OTACache DNS TTL configuration ------------ #
#


class TestOTACacheDNSTTL:
    """Tests for AIOHTTP_DNS_CACHE_TTL configuration bounds and application."""

    def test_dns_cache_ttl_config_within_reasonable_range(self):
        """AIOHTTP_DNS_CACHE_TTL should be longer than aiohttp default (10s) but
        short enough to allow CDN failover (CloudFront TTL is ~60-300s)."""
        assert cfg.AIOHTTP_DNS_CACHE_TTL > 10, (
            "TTL should be greater than aiohttp default (10s) to reduce DNS resolution frequency"
        )
        assert cfg.AIOHTTP_DNS_CACHE_TTL <= 300, (
            "TTL should not exceed CDN DNS TTL (~300s) to allow IP failover"
        )

    async def test_ota_cache_start_applies_dns_cache_ttl(self):
        """OTACache.start() should configure TCPConnector with AIOHTTP_DNS_CACHE_TTL."""
        cache = OTACache(
            cache_enabled=False,
            init_cache=False,
            shm_metrics_writer=None,
        )
        with patch.object(
            aiohttp, "TCPConnector", wraps=aiohttp.TCPConnector
        ) as mock_connector:
            await cache.start()
            try:
                mock_connector.assert_called_once_with(
                    ttl_dns_cache=cfg.AIOHTTP_DNS_CACHE_TTL
                )
            finally:
                await cache.close()


#
# ------------ Tests: OTACache NFS cache lookup ordering ------------ #
#


class TestOTACacheNfsCacheLookupOrder:
    """Tests for NFS vs local cache lookup ordering in retrieve_file."""

    async def test_local_cache_checked_before_nfs(
        self, tmp_path_factory: pytest.TempPathFactory
    ):
        """Local cache lookup must be attempted before NFS cache."""
        nfs_mount = tmp_path_factory.mktemp("nfs_cache")

        with patch(
            "ota_proxy.ota_cache.cmdhelper.is_target_mounted", return_value=True
        ):
            cache = OTACache(
                cache_enabled=True,
                init_cache=False,
                base_dir=tmp_path_factory.mktemp("ota-cache") / "local_cache",
                external_nfs_cache_mnt_point=str(nfs_mount),
            )

            nfs_result = (AsyncMock(), CIMultiDict({"source": "nfs"}))

            with patch.object(
                cache, "_retrieve_file_by_cache_lookup", new_callable=AsyncMock
            ) as mock_local, patch.object(
                cache, "_retrieve_file_by_external_cache", new_callable=AsyncMock
            ) as mock_nfs, patch.object(
                cache, "_retrieve_file_by_downloading", new_callable=AsyncMock
            ):
                mock_local.return_value = None
                mock_nfs.return_value = nfs_result

                await cache.start()
                try:
                    result = await cache.retrieve_file(
                        "http://example.com/file", CIMultiDict()
                    )

                    mock_local.assert_called_once()
                    mock_nfs.assert_called_once()
                    assert result == nfs_result
                finally:
                    await cache.close()

    async def test_local_cache_preferred_over_nfs(
        self, tmp_path_factory: pytest.TempPathFactory
    ):
        """Local cache hit must short-circuit NFS cache lookup."""
        cache = OTACache(
            cache_enabled=True,
            init_cache=False,
            base_dir=tmp_path_factory.mktemp("ota-cache") / "local_cache",
            external_nfs_cache_mnt_point="/mnt/nfs_cache",
        )

        local_result = (AsyncMock(), CIMultiDict({"source": "local"}))
        nfs_result = (AsyncMock(), CIMultiDict({"source": "nfs"}))

        with patch.object(
            cache, "_retrieve_file_by_cache_lookup", new_callable=AsyncMock
        ) as mock_local, patch.object(
            cache, "_retrieve_file_by_external_cache", new_callable=AsyncMock
        ) as mock_nfs, patch.object(
            cache, "_retrieve_file_by_downloading", new_callable=AsyncMock
        ):
            mock_local.return_value = local_result
            mock_nfs.return_value = nfs_result

            await cache.start()
            try:
                result = await cache.retrieve_file(
                    "http://example.com/file", CIMultiDict()
                )

                mock_local.assert_called_once()
                mock_nfs.assert_not_called()
                assert result == local_result
            finally:
                await cache.close()

    async def test_local_cache_hit_skips_nfs(
        self, tmp_path_factory: pytest.TempPathFactory
    ):
        """When only local cache has the file, NFS should not be consulted."""
        cache = OTACache(
            cache_enabled=True,
            init_cache=False,
            base_dir=tmp_path_factory.mktemp("ota-cache") / "local_cache",
            external_nfs_cache_mnt_point="/mnt/nfs_cache",
        )

        local_result = (AsyncMock(), CIMultiDict({"source": "local"}))

        with patch.object(
            cache, "_retrieve_file_by_cache_lookup", new_callable=AsyncMock
        ) as mock_local, patch.object(
            cache, "_retrieve_file_by_external_cache", new_callable=AsyncMock
        ) as mock_nfs, patch.object(
            cache, "_retrieve_file_by_downloading", new_callable=AsyncMock
        ):
            mock_local.return_value = local_result
            mock_nfs.return_value = None

            await cache.start()
            try:
                result = await cache.retrieve_file(
                    "http://example.com/file", CIMultiDict()
                )

                mock_local.assert_called_once()
                mock_nfs.assert_not_called()
                assert result == local_result
            finally:
                await cache.close()
