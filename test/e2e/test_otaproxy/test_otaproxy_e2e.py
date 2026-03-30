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
Multiple concurrent clients (3) hit the proxy simultaneously.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import sqlite3
import time
from pathlib import Path

from ota_proxy.config import config as ota_proxy_cfg

from ._download_client import DownloadResult
from .conftest import (
    CONCURRENT_START_DELAY,
    SPACE_CONDITION_BELOW_SOFT,
    RunDownloadClient,
)

logger = logging.getLogger(__name__)

CONCURRENT_CLIENTS = 3


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


async def _run_concurrent_clients(
    run_download_client: RunDownloadClient,
    proxy_url: str,
    ota_image_server: str,
    n: int = CONCURRENT_CLIENTS,
) -> list[DownloadResult]:
    """Launch n download clients that all start downloading at the same moment.

    Each subprocess is spawned immediately but waits until a shared future
    unix timestamp before issuing any HTTP requests.
    """
    start_at = time.time() + CONCURRENT_START_DELAY
    tasks = [
        run_download_client(proxy_url, ota_image_server, start_at=start_at)
        for _ in range(n)
    ]
    return await asyncio.gather(*tasks)


def _check_cache_db(cache_dir: Path, min_entries: int) -> None:
    """Verify the cache sqlite db has entries with valid metadata."""
    db_path = cache_dir / "cache_db"
    assert db_path.is_file(), f"Cache db not found at {db_path}"

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(f"SELECT * FROM {ota_proxy_cfg.TABLE_NAME}")
        rows = cur.fetchall()

    assert len(rows) >= min_entries, (
        f"Expected at least {min_entries} cache db entries, got {len(rows)}"
    )

    now = time.time()
    for row in rows:
        assert row["file_sha256"], f"Empty file_sha256 in row: {dict(row)}"
        assert row["url"], f"Empty url in row: {dict(row)}"
        assert row["cache_size"] >= 0, f"Invalid cache_size in row: {dict(row)}"
        assert 0 < row["last_access"] <= now, f"Invalid last_access in row: {dict(row)}"

    logger.info("Cache db: %d entries, all valid", len(rows))


class TestOTAProxyNoCache:
    """Test otaproxy in relay-only mode (no caching)."""

    async def test_download_all_blobs_no_cache(
        self,
        otaproxy_no_cache: str,
        ota_image_server: str,
        run_download_client: RunDownloadClient,
    ):
        """All blobs should be downloadable and intact through the proxy."""
        results = await _run_concurrent_clients(
            run_download_client, otaproxy_no_cache, ota_image_server
        )
        for i, result in enumerate(results):
            _assert_download_ok(result, f"no-cache client#{i}")


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

        results = await _run_concurrent_clients(
            run_download_client, proxy_url, ota_image_server
        )
        for i, result in enumerate(results):
            _assert_download_ok(result, f"cold cache [{condition}] client#{i}")

        cached_files = [
            f
            for f in cache_dir.iterdir()
            if f.is_file() and f.name != "cache_db" and not f.name.startswith("tmp")
        ]

        if condition == SPACE_CONDITION_BELOW_SOFT:
            assert len(cached_files) > 0, (
                "Cache directory is empty after cold cache run under below_soft_limit"
            )
            # Verify cache db entries are valid.
            _check_cache_db(cache_dir, min_entries=1)

        # CacheTracker registers a weakref.finalize to unlink its tmp file.
        # Force GC so all unreferenced trackers are collected and their
        # finalizers run before we check.
        gc.collect()
        tmp_files = list(cache_dir.glob("tmp*"))
        assert not tmp_files, f"[{condition}] Temp files left in cache dir: {tmp_files}"

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

        # First pass: populate cache (cold) with concurrent clients.
        results_cold = await _run_concurrent_clients(
            run_download_client, proxy_url, ota_image_server
        )
        for i, result in enumerate(results_cold):
            _assert_download_ok(result, f"warm/cold pass [{condition}] client#{i}")

        # Second pass: served from warm cache (or upstream fallback).
        results_warm = await _run_concurrent_clients(
            run_download_client, proxy_url, ota_image_server
        )
        for i, result in enumerate(results_warm):
            _assert_download_ok(result, f"warm pass [{condition}] client#{i}")

        if condition == SPACE_CONDITION_BELOW_SOFT:
            # After warm pass, db should still have valid entries.
            _check_cache_db(cache_dir, min_entries=1)

        # CacheTracker uses weakref.finalize to clean up tmp files.
        gc.collect()
        tmp_files = list(cache_dir.glob("tmp*"))
        assert not tmp_files, f"[{condition}] Temp files left in cache dir: {tmp_files}"
