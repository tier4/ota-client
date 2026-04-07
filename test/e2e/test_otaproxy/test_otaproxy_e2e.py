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
import time
from pathlib import Path

from pytest_mock import MockerFixture

from ._download_client import DownloadResult
from .conftest import (
    CONCURRENT_START_DELAY,
    SPACE_CONDITION_BELOW_SOFT,
    RunDownloadClient,
    _check_cache_db,
    launch_otaproxy_with_cache,
)

logger = logging.getLogger(__name__)

CONCURRENT_CLIENTS = 3


def _assert_download_ok(result: DownloadResult, label: str = "") -> None:
    """Assert that a download client result has no failures."""
    prefix = f"[{label}] " if label else ""
    assert not result["failed_downloads"], (
        f"{prefix}Downloads failed: {result['failed_downloads']}\n"
        f"{result['recorded_failed']=}",
    )
    assert not result["hash_mismatches"], (
        f"{prefix}SHA256 mismatches: {result['hash_mismatches']}\n"
        f"{result['recorded_failed']=}",
    )
    if result["recorded_failed"]:
        logger.info(
            f"downloader run has failures but handled: {result['recorded_failed']}"
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


class TestOTAProxyWithCache:
    """Test otaproxy cache enabled path.

    Parametrized over space conditions via the ``otaproxy`` fixture.
    After a cold cache run, a second download should be served from
    cache (under normal pressure) or still succeed via upstream fallback.
    """

    async def test_warm_cache_serves_from_cache(
        self,
        otaproxy: tuple[str, Path, str],
        ota_image_blobs: dict[str, str],
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
            _assert_download_ok(result, f"cold cache pass [{condition}] client#{i}")

        # Second pass: served from warm cache (or upstream fallback).
        # If the condition is BELOW_SOFT, which all caches will be preserved, test full cached downloading
        # without involving the OTA image server.
        if condition == SPACE_CONDITION_BELOW_SOFT:
            logger.info(
                "Full cached downloading with dummy OTA image server under below_soft condition ..."
            )
            results_warm = await _run_concurrent_clients(
                run_download_client, proxy_url, "http://127.0.0.1:65500"
            )
        else:
            results_warm = await _run_concurrent_clients(
                run_download_client, proxy_url, ota_image_server
            )

        for i, result in enumerate(results_warm):
            _assert_download_ok(result, f"warm cache pass [{condition}] client#{i}")

        # CacheTracker uses weakref.finalize to clean up tmp files.
        gc.collect()
        tmp_files = list(cache_dir.glob("tmp*"))
        assert not tmp_files, f"[{condition}] Temp files left in cache dir: {tmp_files}"


class TestOTAProxyPreloadDB:
    """Test otaproxy DB pre-load on restart.

    Validates that after a cold cache run with space always below soft limit,
    restarting otaproxy (init_cache=False) correctly pre-loads the DB
    and serves all blobs from warm cache without hitting upstream.
    """

    async def test_warm_cache_after_restart(
        self,
        cache_dir: Path,
        mocker: MockerFixture,
        ota_image_blobs: dict[str, str],
        ota_image_server: str,
        run_download_client: RunDownloadClient,
    ):
        """After cold cache, restart otaproxy and verify warm cache via DB pre-load."""
        condition = SPACE_CONDITION_BELOW_SOFT

        # --- Phase 1: Cold cache (fresh DB) ---
        async with launch_otaproxy_with_cache(
            cache_dir, mocker, condition, init_cache=True
        ) as (proxy_url, _, _):
            results_cold = await _run_concurrent_clients(
                run_download_client, proxy_url, ota_image_server
            )
            for i, result in enumerate(results_cold):
                _assert_download_ok(result, f"cold cache pass client#{i}")

        # DB should contain all entries after below_soft cold cache.
        _check_cache_db(
            cache_dir,
            min_entries=len(ota_image_blobs),
            resources_digests=set(ota_image_blobs.values()),
        )

        # --- Phase 2: Restart with DB pre-load ---
        async with launch_otaproxy_with_cache(
            cache_dir, mocker, condition, init_cache=False
        ) as (proxy_url, _, _):
            # Warm cache with dummy upstream: all blobs must be served from cache.
            logger.info("Testing warm cache after restart with dummy upstream ...")
            results_warm = await _run_concurrent_clients(
                run_download_client, proxy_url, "http://127.0.0.1:65500"
            )
            for i, result in enumerate(results_warm):
                _assert_download_ok(result, f"warm cache after restart client#{i}")

        gc.collect()
        tmp_files = list(cache_dir.glob("tmp*"))
        assert not tmp_files, f"Temp files left in cache dir: {tmp_files}"
