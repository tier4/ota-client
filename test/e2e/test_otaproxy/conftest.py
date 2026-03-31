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
"""Fixtures for OTA proxy e2e tests.

Provides:
    - ota_image_blobs: Dict of filename -> sha256 for all blobs (including special).
    - ota_image_server: Launches standalone HTTP server serving OTA image blobs.
    - otaproxy / otaproxy_no_cache: In-process otaproxy with/without caching.
    - run_download_client: Helper to launch the standalone download client subprocess.
"""

from __future__ import annotations

import asyncio
import heapq
import json
import logging
import random
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Coroutine
from hashlib import sha256
from pathlib import Path
from typing import AsyncGenerator, Callable, Generator

import pytest
import uvicorn

from ota_proxy import App, OTACache

from ._download_client import DownloadResult

logger = logging.getLogger(__name__)

OTA_IMAGE_BLOBS_DIR = Path("/ota-image_v1/blobs/sha256")
OTA_IMAGE_SERVER_PORT = 18888
OTAPROXY_PORT = 18080
OTAPROXY_PORT_NOCACHE = 18081

DOWNLOAD_TIMEOUT = 360  # seconds

# Due to the test time too long for downloading the whole image,
#   we only sample to download 1/3 of the total blobs.
DOWNLOAD_ENTRIES_SAMPLE_RATIO = 1 / 3

# Filenames with characters that require URL escaping or are otherwise special.
# These exercise backward compatibility with old OTA images.
SPECIAL_FILENAMES = [
    "file with spaces/file with spaces.bin",
    "file#hash/file#hash.bin",
    "file%percent/file%percent.bin",
    "file+plus/file+plus.bin",
    "file&ampersand/file&ampersand.bin",
    "file=equals/file=equals.bin",
    "file[bracket]/file[bracket].bin",
    "filéàccénted/filéàccénted.bin",
    "fileñtilde/fileñtilde.bin",
    "file@at!exclaim/file@at!exclaim.bin",
]

DOWNLOAD_CLIENT_SCRIPT = Path(__file__).parent / "_download_client.py"
HTTP_SERVER_SCRIPT = Path(__file__).parent / "_ota_image_server.py"

# Space availability conditions for cache rotation testing.
# Each condition simulates a different disk pressure scenario by
# monkeypatching OTACache._background_check_free_space.
SPACE_CONDITION_BELOW_SOFT = "below_soft_limit"
SPACE_CONDITION_BELOW_HARD = "below_hard_limit"
SPACE_CONDITION_EXCEED_HARD = "exceed_hard_limit"


def _wait_for_ready(proc: subprocess.Popen, timeout: float = 30) -> None:
    """Wait for the subprocess to print its READY line."""
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"Process exited prematurely with code {proc.returncode}"
                )
            time.sleep(0.1)
            continue
        if line.startswith("READY:"):
            return
    raise TimeoutError("Timed out waiting for subprocess READY signal")


ENSURE_LARGE_BLOB_ENTRIES = 10


@pytest.fixture(scope="session")
def ota_image_blobs() -> dict[str, str]:
    """Collect all blob files and their SHA256 digests.

    For regular blobs the filename itself is the sha256 hash.
    For special-name files the sha256 is computed from file content.

    Returns:
        A dict mapping filename to its expected sha256 hex digest.
    """
    resources_to_download: dict[str, str] = {}
    # first create special files
    for name in SPECIAL_FILENAMES:
        fpath = OTA_IMAGE_BLOBS_DIR / Path(name)
        fpath.parent.mkdir(exist_ok=True, parents=True)

        _data = f"test-payload-for-{name}".encode("utf-8")
        fpath.write_bytes(_data)
        resources_to_download[name] = sha256(_data).hexdigest()

    logger.info(
        f"Created {len(SPECIAL_FILENAMES)} special files in {OTA_IMAGE_BLOBS_DIR}"
    )

    # collect regular blobs (filename is the sha256 hex digest)
    _blobs, _count = [], 0
    for fpath in OTA_IMAGE_BLOBS_DIR.iterdir():
        _fname, _fsize = fpath.name, fpath.stat().st_size
        if _fname in resources_to_download:
            continue  # already registered (special file)

        try:
            bytes.fromhex(_fname)
        except ValueError:
            continue  # not a hex-named blob, skip

        _blobs.append((_fname, _fsize, _fname))
        _count += 1

    _top_large_blobs = heapq.nlargest(
        ENSURE_LARGE_BLOB_ENTRIES, _blobs, key=lambda x: x[1]
    )

    logger.info(f"top {ENSURE_LARGE_BLOB_ENTRIES} entries: {_top_large_blobs}")

    _sample_counts = int(_count * DOWNLOAD_ENTRIES_SAMPLE_RATIO)
    resources_to_download.update(
        (_entry[0], _entry[-1]) for _entry in random.sample(_blobs, _sample_counts)
    )
    resources_to_download.update(
        ((_entry[0], _entry[-1]) for _entry in _top_large_blobs)
    )

    logger.info(
        f"Collected {len(resources_to_download)} files from {OTA_IMAGE_BLOBS_DIR} (special counts: {len(SPECIAL_FILENAMES)})"
    )
    return resources_to_download


@pytest.fixture(scope="session")
def ota_image_server(ota_image_blobs) -> Generator[str]:
    """Launch the standalone OTA image HTTP server.

    Returns:
        The base URL of the server (e.g. "http://127.0.0.1:18888").
    """
    proc = subprocess.Popen(
        [
            sys.executable,
            str(HTTP_SERVER_SCRIPT),
            "--port",
            str(OTA_IMAGE_SERVER_PORT),
            "--directory",
            str(OTA_IMAGE_BLOBS_DIR),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_ready(proc)
        base_url = f"http://127.0.0.1:{OTA_IMAGE_SERVER_PORT}"
        logger.info("OTA image server started at %s", base_url)
        yield base_url
    finally:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=10)
        logger.info("OTA image server stopped")


@pytest.fixture()
def cache_dir(tmp_path: Path) -> Generator[Path]:
    """Provide a temporary cache directory for otaproxy."""
    d = tmp_path / "ota-cache"
    d.mkdir()
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _resolve_space_state(condition: str, tick: int) -> tuple[bool, bool]:
    """Return (below_soft, below_hard) for a given condition and tick.

    Each condition defines a timeline of disk pressure transitions:
      - below_soft_limit: always normal.
      - below_hard_limit: normal for 10 ticks, then soft limit exceeded.
      - exceed_hard_limit: normal for 5 ticks, soft exceeded for 5 more,
        then both exceeded.
    """
    # (below_soft_limit_set, below_hard_limit_set)
    _TRANSITIONS: dict[str, list[tuple[int, bool, bool]]] = {
        SPACE_CONDITION_BELOW_SOFT: [(0, True, True)],
        SPACE_CONDITION_BELOW_HARD: [(0, True, True), (8, False, True)],
        SPACE_CONDITION_EXCEED_HARD: [
            (0, True, True),
            (5, False, True),
            (8, False, False),
        ],
    }
    below_soft, below_hard = True, True
    for threshold, soft, hard in _TRANSITIONS[condition]:
        if tick >= threshold:
            below_soft, below_hard = soft, hard
    return below_soft, below_hard


def _make_mocked_space_checker(condition: str):

    def _mocked_background_check_freespace(self):
        _count = 0
        while not self._closed:
            below_soft, below_hard = _resolve_space_state(condition, _count)

            if below_soft:
                self._storage_below_soft_limit_event.set()
            else:
                self._storage_below_soft_limit_event.clear()

            if below_hard:
                self._storage_below_hard_limit_event.set()
            else:
                self._storage_below_hard_limit_event.clear()

            time.sleep(2)
            _count += 1

    return _mocked_background_check_freespace


async def _launch_otaproxy(
    port: int, ota_cache: OTACache
) -> tuple[str, uvicorn.Server, asyncio.Task]:
    """Start otaproxy in-process and return its URL, server, and task.

    Args:
        port: TCP port to listen on.
        ota_cache: Pre-configured OTACache instance.

    Returns:
        A tuple of (proxy_url, server, serve_task).
    """
    app = App(ota_cache)
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        lifespan="on",
        http="h11",
    )
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())

    while not server.started:
        await asyncio.sleep(0.05)

    proxy_url = f"http://127.0.0.1:{port}"
    logger.info("otaproxy started at %s", proxy_url)
    return proxy_url, server, serve_task


@pytest.fixture(
    params=[
        SPACE_CONDITION_BELOW_SOFT,
        SPACE_CONDITION_BELOW_HARD,
        SPACE_CONDITION_EXCEED_HARD,
    ]
)
async def otaproxy(
    request, ota_image_server: str, cache_dir: Path
) -> AsyncGenerator[tuple[str, Path, str]]:
    """Run otaproxy in-process with caching enabled.

    Parametrized over disk space conditions to test cache rotation.

    Yields:
        A tuple of (proxy_url, cache_dir, space_condition).
    """
    condition: str = request.param

    # Monkeypatch the space checker before OTACache is started.
    _orig = OTACache._background_check_free_space
    OTACache._background_check_free_space = _make_mocked_space_checker(condition)

    ota_cache = OTACache(
        cache_enabled=True,
        init_cache=True,
        base_dir=str(cache_dir),
        db_file=str(cache_dir / "cache_db"),
        upper_proxy="",
        enable_https=False,
    )

    proxy_url, server, server_task = await _launch_otaproxy(OTAPROXY_PORT, ota_cache)
    logger.info("otaproxy space condition: %s", condition)
    yield proxy_url, cache_dir, condition

    server.should_exit = True
    await server_task
    OTACache._background_check_free_space = _orig
    logger.info("otaproxy stopped (port %d, condition=%s)", OTAPROXY_PORT, condition)


@pytest.fixture()
async def otaproxy_no_cache(ota_image_server: str) -> AsyncGenerator[str]:
    """Run otaproxy in-process without caching.

    Returns:
        The proxy URL.
    """
    ota_cache = OTACache(
        cache_enabled=False, init_cache=False, upper_proxy="", enable_https=False
    )
    proxy_url, server, server_task = await _launch_otaproxy(OTAPROXY_PORT, ota_cache)
    yield proxy_url

    server.should_exit = True
    await server_task
    logger.info("otaproxy (no cache) stopped")


@pytest.fixture(scope="session")
def manifest_path(
    ota_image_blobs: dict[str, str], tmp_path_factory: pytest.TempPathFactory
) -> Path:
    """Write the blob manifest as a JSON file for the download client."""
    p = tmp_path_factory.mktemp("manifest") / "manifest.json"
    p.write_text(json.dumps(ota_image_blobs))
    logger.info("Manifest written to %s (%d entries)", p, len(ota_image_blobs))
    return p


CONCURRENT_START_DELAY = 3  # seconds into the future for synchronized start


async def _run_download_client(
    proxy_url: str,
    upstream_url: str,
    manifest: Path,
    *,
    headers: dict[str, str] | None = None,
    start_at: float = 0,
    _timeout: float,
) -> DownloadResult:
    """Launch the download client subprocess and return parsed results.

    This is async so that the event loop keeps running the in-process
    otaproxy while the download client subprocess executes.

    Raises:
        AssertionError on non-zero exit or unparseable output.
    """
    cmd = [
        sys.executable,
        str(DOWNLOAD_CLIENT_SCRIPT),
        "--proxy-url",
        proxy_url,
        "--upstream-url",
        upstream_url,
        "--manifest",
        str(manifest),
    ]
    if start_at > 0:
        cmd.extend(["--start-at", str(start_at)])
    for k, v in (headers or {}).items():
        cmd.extend(["--header", f"{k}: {v}"])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise

    stdout_str = stdout.decode()
    assert stdout_str, (
        f"Download client produced no output (rc={proc.returncode}).\n"
        f"stderr: {stderr.decode()}"
    )
    return json.loads(stdout_str)


RunDownloadClient = Callable[..., "Coroutine[None, None, DownloadResult]"]


@pytest.fixture()
def run_download_client(manifest_path: Path) -> RunDownloadClient:
    """Fixture providing an async callable to run the download client.

    Usage in tests::

        result = await run_download_client(proxy_url, upstream_url)
        result = await run_download_client(proxy_url, upstream_url, headers={...})
    """

    async def _run(
        proxy_url: str,
        upstream_url: str,
        *,
        manifest: Path | None = None,
        headers: dict[str, str] | None = None,
        start_at: float = 0,
        _timeout: float = DOWNLOAD_TIMEOUT,
    ) -> DownloadResult:
        return await _run_download_client(
            proxy_url,
            upstream_url,
            manifest or manifest_path,
            headers=headers,
            start_at=start_at,
            _timeout=_timeout,
        )

    return _run
