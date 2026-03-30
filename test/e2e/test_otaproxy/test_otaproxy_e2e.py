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
"""End-to-end tests for OTA proxy.

Test paths covered:
    1. No cache   - otaproxy relays data without caching.
    2. Cold cache - otaproxy downloads from upstream and populates cache.
    3. Warm cache - otaproxy serves from local cache (no upstream hit).

otaproxy runs **in-process** (managed by async fixtures).
The download client runs as a **standalone subprocess** that downloads
all blobs through the proxy and validates SHA256 integrity.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .conftest import SPECIAL_FILENAMES, RunDownloadClient
from .download_client import DownloadResult

logger = logging.getLogger(__name__)


def _assert_download_ok(result: DownloadResult, label: str = "") -> None:
    """Assert that a download client result has no failures."""
    prefix = f"[{label}] " if label else ""
    assert not result["failed_downloads"], (
        f"{prefix}Downloads failed: {result['failed_downloads']}"
    )
    assert not result["hash_mismatches"], (
        f"{prefix}SHA256 mismatches: {result['hash_mismatches']}"
    )
    logger.info("%s%d/%d blobs OK", prefix, result["ok"], result["total"])


def _write_special_manifest(ota_image_blobs: dict[str, str], tmp_path: Path) -> Path:
    """Write a manifest containing only the special-filename blobs."""
    special = {k: v for k, v in ota_image_blobs.items() if k in SPECIAL_FILENAMES}
    p = tmp_path / "special_manifest.json"
    p.write_text(json.dumps(special))
    return p


class TestOTAProxyNoCache:
    """Test otaproxy in relay-only mode (no caching)."""

    async def test_download_all_blobs_no_cache(
        self,
        otaproxy_no_cache: str,
        ota_image_server: str,
        run_download_client: RunDownloadClient,
    ):
        """All blobs should be downloadable and intact through the proxy."""
        result = await run_download_client(otaproxy_no_cache, ota_image_server)
        _assert_download_ok(result, "no-cache")

    async def test_special_filenames_no_cache(
        self,
        otaproxy_no_cache: str,
        ota_image_server: str,
        run_download_client: RunDownloadClient,
        ota_image_blobs: dict[str, str],
        tmp_path: Path,
    ):
        """Files with special characters should be downloadable and intact."""
        manifest = _write_special_manifest(ota_image_blobs, tmp_path)
        result = await run_download_client(
            otaproxy_no_cache, ota_image_server, manifest=manifest
        )
        _assert_download_ok(result, "no-cache special")


class TestOTAProxyColdCache:
    """Test otaproxy cold cache write path.

    When the cache is empty, otaproxy should download from upstream,
    cache the data, and serve it to the client.
    """

    async def test_cold_cache_populates_cache_dir(
        self,
        otaproxy: tuple[str, Path],
        ota_image_server: str,
        run_download_client: RunDownloadClient,
    ):
        """Downloaded blobs should be intact and cache directory populated."""
        proxy_url, cache_dir = otaproxy
        result = await run_download_client(proxy_url, ota_image_server)
        _assert_download_ok(result, "cold cache")

        cached_files = [
            f
            for f in cache_dir.iterdir()
            if f.is_file() and f.name != "cache_db" and not f.name.startswith("tmp")
        ]
        assert len(cached_files) > 0, "Cache directory is empty after cold cache run"
        logger.info("Cold cache: %d cache entries created", len(cached_files))

    async def test_cold_cache_special_filenames(
        self,
        otaproxy: tuple[str, Path],
        ota_image_server: str,
        run_download_client: RunDownloadClient,
        ota_image_blobs: dict[str, str],
        tmp_path: Path,
    ):
        """Files with special names should be downloadable, intact, and cached."""
        proxy_url, _ = otaproxy
        manifest = _write_special_manifest(ota_image_blobs, tmp_path)
        result = await run_download_client(
            proxy_url, ota_image_server, manifest=manifest
        )
        _assert_download_ok(result, "cold cache special")


class TestOTAProxyWarmCache:
    """Test otaproxy warm cache path.

    After a cold cache run, a second download of the same blobs should
    be served from cache with identical content.
    """

    async def test_warm_cache_serves_from_cache(
        self,
        otaproxy: tuple[str, Path],
        ota_image_server: str,
        run_download_client: RunDownloadClient,
    ):
        """Second download run should be served from cache and validate OK."""
        proxy_url, _ = otaproxy

        # First pass: populate cache (cold).
        result_cold = await run_download_client(proxy_url, ota_image_server)
        _assert_download_ok(result_cold, "warm/cold pass")

        # Second pass: served from warm cache.
        result_warm = await run_download_client(proxy_url, ota_image_server)
        _assert_download_ok(result_warm, "warm pass")

    async def test_warm_cache_special_filenames(
        self,
        otaproxy: tuple[str, Path],
        ota_image_server: str,
        run_download_client: RunDownloadClient,
        ota_image_blobs: dict[str, str],
        tmp_path: Path,
    ):
        """Special filename blobs should also validate from warm cache."""
        proxy_url, _ = otaproxy
        manifest = _write_special_manifest(ota_image_blobs, tmp_path)

        # Cold pass.
        await run_download_client(proxy_url, ota_image_server, manifest=manifest)

        # Warm pass.
        result = await run_download_client(
            proxy_url, ota_image_server, manifest=manifest
        )
        _assert_download_ok(result, "warm cache special")
