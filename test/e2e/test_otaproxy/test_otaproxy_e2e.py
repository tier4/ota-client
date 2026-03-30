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

Each cached test path is parametrized over disk space conditions
(below_soft_limit, below_hard_limit, exceed_hard_limit) to exercise
LRU cache rotation and cache disabling.

otaproxy runs **in-process** (managed by async fixtures).
The download client runs as a **standalone subprocess** that downloads
all blobs through the proxy and validates SHA256 integrity.
"""

from __future__ import annotations

import gc
import logging
from pathlib import Path

from ._download_client import DownloadResult
from .conftest import (
    SPACE_CONDITION_BELOW_SOFT,
    RunDownloadClient,
)

logger = logging.getLogger(__name__)


def _assert_download_ok(result: DownloadResult, label: str = "") -> None:
    """Assert that a download client result has no failures."""
    prefix = f"[{label}] " if label else ""
    assert not result[
        "failed_downloads"
    ], f"{prefix}Downloads failed: {result['failed_downloads']}"
    assert not result[
        "hash_mismatches"
    ], f"{prefix}SHA256 mismatches: {result['hash_mismatches']}"
    logger.info("%s%d/%d blobs OK", prefix, result["ok"], result["total"])


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


class TestOTAProxyColdCache:
    """Test otaproxy cold cache write path.

    Parametrized over space conditions via the ``otaproxy`` fixture.
    When the cache is empty, otaproxy downloads from upstream,
    caches data (subject to space limits), and serves it to the client.
    """

    async def test_cold_cache_populates_cache_dir(
        self,
        otaproxy: tuple[str, Path, str],
        ota_image_server: str,
        run_download_client: RunDownloadClient,
    ):
        """Downloaded blobs should be intact; cache populated under normal pressure."""
        proxy_url, cache_dir, condition = otaproxy
        result = await run_download_client(proxy_url, ota_image_server)
        _assert_download_ok(result, f"cold cache [{condition}]")

        cached_files = [
            f
            for f in cache_dir.iterdir()
            if f.is_file()
            and f.name != "cache_db"
            and not f.name.startswith("tmp")
        ]

        if condition == SPACE_CONDITION_BELOW_SOFT:
            assert len(cached_files) > 0, (
                "Cache directory is empty after cold cache run under below_soft_limit"
            )

        # CacheTracker registers a weakref.finalize to unlink its tmp file.
        # Force GC so all unreferenced trackers are collected and their
        # finalizers run before we check.
        gc.collect()
        tmp_files = list(cache_dir.glob("tmp*"))
        assert not tmp_files, (
            f"[{condition}] Temp files left in cache dir: {tmp_files}"
        )

        logger.info(
            "Cold cache [%s]: %d cache entries",
            condition,
            len(cached_files),
        )


class TestOTAProxyWarmCache:
    """Test otaproxy warm cache path.

    Parametrized over space conditions via the ``otaproxy`` fixture.
    After a cold cache run, a second download should be served from
    cache (under normal pressure) or still succeed via upstream fallback.
    """

    async def test_warm_cache_serves_from_cache(
        self,
        otaproxy: tuple[str, Path, str],
        ota_image_server: str,
        run_download_client: RunDownloadClient,
    ):
        """Second download run should succeed regardless of space condition."""
        proxy_url, cache_dir, condition = otaproxy

        # First pass: populate cache (cold).
        result_cold = await run_download_client(proxy_url, ota_image_server)
        _assert_download_ok(result_cold, f"warm/cold pass [{condition}]")

        # Second pass: served from warm cache (or upstream fallback).
        result_warm = await run_download_client(proxy_url, ota_image_server)
        _assert_download_ok(result_warm, f"warm pass [{condition}]")

        # CacheTracker uses weakref.finalize to clean up tmp files.
        gc.collect()
        tmp_files = list(cache_dir.glob("tmp*"))
        assert not tmp_files, (
            f"[{condition}] Temp files left in cache dir: {tmp_files}"
        )
