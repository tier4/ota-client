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
"""End-to-end tests for OTA proxy with multi-layer proxy chain.

Test topology::

    image_server <-- gateway_proxy <-- sub_proxy_0 <-- client_0
                                   <-- sub_proxy_1 <-- client_1
                                   <-- sub_proxy_2 <-- client_2

The gateway proxy faces the image server directly (no upper proxy).
Each sub proxy uses the gateway as its upper proxy.
Each download client routes through its own sub proxy.

otaproxy instances run **in-process** (managed by async context managers).
The download client runs as a **standalone subprocess** that downloads
all blobs through the proxy and validates SHA256 integrity.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import time
from contextlib import AsyncExitStack
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from ._download_client import DownloadResult
from .conftest import (
    CONCURRENT_START_DELAY,
    SPACE_CONDITION_BELOW_HARD,
    SPACE_CONDITION_BELOW_SOFT,
    SPACE_CONDITION_EXCEED_HARD,
    RunDownloadClient,
    check_cache_db,
    launch_otaproxy_no_cache,
    launch_otaproxy_with_cache,
)

logger = logging.getLogger(__name__)

# Ports for the multi-layer proxy chain test.
GATEWAY_PORT = 18090
SUB_PROXY_PORTS = [18091, 18092, 18093]
SUB_PROXY_COUNT = len(SUB_PROXY_PORTS)
DUMMY_OTA_IMAGE_SERVER = "http://127.0.0.1:65500"


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
    logger.info(f"{prefix}{result['ok']}/{result['total']} blobs OK")


class TestOTAProxyChain:
    """Test multi-layer otaproxy chain.

    Topology::

        image_server <-- gateway_proxy <-- sub_proxy_0 <-- client_0
                                       <-- sub_proxy_1 <-- client_1
                                       <-- sub_proxy_2 <-- client_2

    The gateway proxy faces the image server directly (no upper proxy).
    Each sub proxy uses the gateway as its upper proxy.
    Each download client routes through its own sub proxy.

    Validates that downloads succeed through a two-hop proxy chain
    and that caching works correctly at every layer.
    """

    @pytest.mark.parametrize(
        "condition",
        [
            SPACE_CONDITION_BELOW_SOFT,
            SPACE_CONDITION_BELOW_HARD,
            SPACE_CONDITION_EXCEED_HARD,
        ],
    )
    async def test_proxy_chain_cold_and_warm_cache(
        self,
        condition: str,
        tmp_path: Path,
        mocker: MockerFixture,
        ota_image_blobs: dict[str, str],
        ota_image_server: str,
        run_download_client: RunDownloadClient,
    ):
        """Downloads through a two-hop proxy chain populate caches at each layer.

        Parametrized over disk space conditions to verify threshold-based
        behavior: persist below the soft limit, skip persistence above the
        soft limit, and proxy directly above the hard limit.
        """
        # Create separate cache directories for each proxy.
        gateway_cache_dir = tmp_path / "gateway-cache"
        gateway_cache_dir.mkdir()
        sub_cache_dirs = []
        for i in range(SUB_PROXY_COUNT):
            d = tmp_path / f"sub-cache-{i}"
            d.mkdir()
            sub_cache_dirs.append(d)

        async with AsyncExitStack() as stack:
            # Layer 1: Gateway proxy (faces image server directly).
            gateway_url, _, _ = await stack.enter_async_context(
                launch_otaproxy_with_cache(
                    gateway_cache_dir,
                    mocker,
                    condition,
                    port=GATEWAY_PORT,
                )
            )

            # Layer 2: Sub proxies (each uses gateway as upper proxy).
            sub_urls = []
            for i, port in enumerate(SUB_PROXY_PORTS):
                sub_url, _, _ = await stack.enter_async_context(
                    launch_otaproxy_with_cache(
                        sub_cache_dirs[i],
                        mocker,
                        condition,
                        port=port,
                        upper_proxy=gateway_url,
                    )
                )
                sub_urls.append(sub_url)

            # --- Phase 1: Cold cache ---
            # Each client downloads all blobs through its own sub proxy.
            start_at = time.time() + CONCURRENT_START_DELAY
            cold_tasks = [
                run_download_client(sub_url, ota_image_server, start_at=start_at)
                for sub_url in sub_urls
            ]
            results_cold = await asyncio.gather(*cold_tasks)
            for i, result in enumerate(results_cold):
                _assert_download_ok(result, f"cold [{condition}] sub#{i}")

            # --- Phase 2: Warm cache ---
            # Under below_soft all caches are preserved, so use a dummy
            # upstream to prove data is served entirely from cache.
            # Under other conditions some entries may be evicted by cache
            # rotation, so fall back to the real upstream.
            if condition == SPACE_CONDITION_BELOW_SOFT:
                logger.info(
                    f"Full cached downloading with dummy upstream under {condition} ..."
                )
                warm_upstream = DUMMY_OTA_IMAGE_SERVER
            else:
                warm_upstream = ota_image_server

            start_at = time.time() + CONCURRENT_START_DELAY
            warm_tasks = [
                run_download_client(sub_url, warm_upstream, start_at=start_at)
                for sub_url in sub_urls
            ]
            results_warm = await asyncio.gather(*warm_tasks)
            for i, result in enumerate(results_warm):
                _assert_download_ok(result, f"warm [{condition}] sub#{i}")

        # Under below_soft, all cache DBs should have every entry.
        if condition == SPACE_CONDITION_BELOW_SOFT:
            _unique_digests = set(ota_image_blobs.values())
            check_cache_db(
                gateway_cache_dir,
                min_entries=len(_unique_digests),
                resources_digests=_unique_digests,
            )
            for sub_cache_dir in sub_cache_dirs:
                check_cache_db(
                    sub_cache_dir,
                    min_entries=len(_unique_digests),
                    resources_digests=_unique_digests,
                )

        # Verify no temp files left in any cache directory.
        gc.collect()
        for d in [gateway_cache_dir, *sub_cache_dirs]:
            tmp_files = list(d.glob("tmp*"))
            assert not tmp_files, f"[{condition}] Temp files left in {d}: {tmp_files}"

    async def test_proxy_chain_no_cache(
        self,
        ota_image_server: str,
        run_download_client: RunDownloadClient,
    ):
        """All blobs should be downloadable through a relay-only proxy chain."""
        async with AsyncExitStack() as stack:
            # Layer 1: Gateway proxy (no cache, faces image server directly).
            gateway_url = await stack.enter_async_context(
                launch_otaproxy_no_cache(port=GATEWAY_PORT)
            )

            # Layer 2: Sub proxies (no cache, each uses gateway as upper proxy).
            sub_urls = []
            for port in SUB_PROXY_PORTS:
                sub_url = await stack.enter_async_context(
                    launch_otaproxy_no_cache(port=port, upper_proxy=gateway_url)
                )
                sub_urls.append(sub_url)

            # Each client downloads all blobs through its own sub proxy.
            start_at = time.time() + CONCURRENT_START_DELAY
            tasks = [
                run_download_client(sub_url, ota_image_server, start_at=start_at)
                for sub_url in sub_urls
            ]
            results = await asyncio.gather(*tasks)
            for i, result in enumerate(results):
                _assert_download_ok(result, f"no-cache chain sub#{i}")


class TestOTAProxyPreloadDB:
    """Test otaproxy DB pre-load on restart.

    Validates that after a cold cache run with space always below soft limit,
    restarting otaproxy (init_cache=False) correctly pre-loads the DB
    and serves all blobs from warm cache without hitting upstream.
    """

    async def test_warm_cache_after_restart(
        self,
        tmp_path: Path,
        mocker: MockerFixture,
        ota_image_blobs: dict[str, str],
        ota_image_server: str,
        run_download_client: RunDownloadClient,
    ):
        """After cold cache, restart otaproxy and verify warm cache via DB pre-load."""
        condition = SPACE_CONDITION_BELOW_SOFT
        cache_dir = tmp_path / "ota-cache"
        cache_dir.mkdir()

        # --- Phase 1: Cold cache (fresh DB) ---
        async with launch_otaproxy_with_cache(
            cache_dir, mocker, condition, init_cache=True
        ) as (proxy_url, _, _):
            start_at = time.time() + CONCURRENT_START_DELAY
            tasks = [
                run_download_client(proxy_url, ota_image_server, start_at=start_at)
                for _ in range(SUB_PROXY_COUNT)
            ]
            results_cold = await asyncio.gather(*tasks)
            for i, result in enumerate(results_cold):
                _assert_download_ok(result, f"cold cache pass client#{i}")

        # DB should contain all entries after below_soft cold cache.
        _unique_digests = set(ota_image_blobs.values())
        check_cache_db(
            cache_dir,
            min_entries=len(_unique_digests),
            resources_digests=_unique_digests,
        )

        # --- Phase 2: Restart with DB pre-load ---
        async with launch_otaproxy_with_cache(
            cache_dir, mocker, condition, init_cache=False
        ) as (proxy_url, _, _):
            # Warm cache with dummy upstream: all blobs must be served from cache.
            logger.info("Testing warm cache after restart with dummy upstream ...")
            start_at = time.time() + CONCURRENT_START_DELAY
            tasks = [
                run_download_client(
                    proxy_url, DUMMY_OTA_IMAGE_SERVER, start_at=start_at
                )
                for _ in range(SUB_PROXY_COUNT)
            ]
            results_warm = await asyncio.gather(*tasks)
            for i, result in enumerate(results_warm):
                _assert_download_ok(result, f"warm cache after restart client#{i}")

        gc.collect()
        tmp_files = list(cache_dir.glob("tmp*"))
        assert not tmp_files, f"Temp files left in cache dir: {tmp_files}"
