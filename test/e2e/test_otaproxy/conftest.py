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
    - special_filenames: Creates files with special names for backward compat testing.
    - ota_image_blobs: Dict of filename -> sha256 for all blobs (including special).
    - ota_image_server: Launches standalone HTTP server serving OTA image blobs.
    - otaproxy / otaproxy_no_cache: In-process otaproxy with/without caching.
    - run_download_client: Helper to launch the standalone download client subprocess.
"""

from __future__ import annotations

import asyncio
import json
import logging
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

# Filenames with characters that require URL escaping or are otherwise special.
# These exercise backward compatibility with old OTA images.
SPECIAL_FILENAMES = [
    "file with spaces.bin",
    "file#hash.bin",
    "file%percent.bin",
    "file+plus.bin",
    "file&ampersand.bin",
    "file=equals.bin",
    "file[bracket].bin",
    "filéàccénted.bin",
    "fileñtilde.bin",
    "file日本語.bin",
    "file@at!exclaim.bin",
]

DOWNLOAD_CLIENT_SCRIPT = Path(__file__).parent / "download_client.py"


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


@pytest.fixture(scope="session")
def ota_image_blobs() -> dict[str, str]:
    """Collect all blob files and their SHA256 digests.

    For regular blobs the filename itself is the sha256 hash.
    For special-name files the sha256 is computed from file content.

    Returns:
        A dict mapping filename to its expected sha256 hex digest.
    """
    blobs: dict[str, str] = {}
    # first create special files
    for name in SPECIAL_FILENAMES:
        fpath = OTA_IMAGE_BLOBS_DIR / name

        _data = f"test-payload-for-{name}".encode("utf-8")
        fpath.write_bytes(_data)
        blobs[name] = sha256(_data).hexdigest()

    logger.info(
        f"Created {len(SPECIAL_FILENAMES)} special files in {OTA_IMAGE_BLOBS_DIR}"
    )

    # check all the blobs
    for fpath in OTA_IMAGE_BLOBS_DIR.iterdir():
        try:
            bytes.fromhex(fpath.name)
        except ValueError:
            pass  # not a blob
        blobs[fpath.name] = fpath.name

    logger.info(
        f"Collected {len(blobs)} files from {OTA_IMAGE_BLOBS_DIR} (special counts: {len(SPECIAL_FILENAMES)})"
    )
    return blobs


@pytest.fixture(scope="session")
def ota_image_server(ota_image_blobs) -> Generator[str]:
    """Launch the standalone OTA image HTTP server.

    Returns:
        The base URL of the server (e.g. "http://127.0.0.1:18888").
    """
    server_script = Path(__file__).parent / "ota_image_server.py"
    proc = subprocess.Popen(
        [
            sys.executable,
            str(server_script),
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
def cache_dir(tmp_path: Path) -> Path:
    """Provide a temporary cache directory for otaproxy."""
    d = tmp_path / "ota-cache"
    d.mkdir()
    return d


async def _launch_otaproxy(
    port: int, ota_cache: OTACache
) -> tuple[str, uvicorn.Server, asyncio.Task]:
    """Start otaproxy in-process and yield its URL, then shut down.

    Args:
        port: TCP port to listen on.
        ota_cache: Pre-configured OTACache instance.

    Yields:
        The proxy base URL (e.g. ``http://127.0.0.1:18080``).
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


@pytest.fixture()
async def otaproxy(
    ota_image_server: str, cache_dir: Path
) -> AsyncGenerator[tuple[str, Path]]:
    """Run otaproxy in-process with caching enabled.

    Returns:
        A tuple of (proxy_url, cache_dir).
    """
    ota_cache = OTACache(
        cache_enabled=True,
        init_cache=True,
        base_dir=str(cache_dir),
        db_file=str(cache_dir / "cache_db"),
        upper_proxy="",
        enable_https=False,
    )

    proxy_url, server, server_task = await _launch_otaproxy(OTAPROXY_PORT, ota_cache)
    yield proxy_url, cache_dir

    server.should_exit = True
    await server_task
    logger.info(f"otaproxy stopped (port {OTAPROXY_PORT})")


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
    logger.info(f"otaproxy stopped (port {OTAPROXY_PORT})")


@pytest.fixture(scope="session")
def manifest_path(
    ota_image_blobs: dict[str, str], tmp_path_factory: pytest.TempPathFactory
) -> Path:
    """Write the blob manifest as a JSON file for the download client."""
    p = tmp_path_factory.mktemp("manifest") / "manifest.json"
    p.write_text(json.dumps(ota_image_blobs))
    logger.info("Manifest written to %s (%d entries)", p, len(ota_image_blobs))
    return p


async def _run_download_client(
    proxy_url: str,
    upstream_url: str,
    manifest: Path,
    *,
    headers: dict[str, str] | None = None,
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
        _timeout: float = DOWNLOAD_TIMEOUT,
    ) -> DownloadResult:
        return await _run_download_client(
            proxy_url,
            upstream_url,
            manifest or manifest_path,
            headers=headers,
            _timeout=_timeout,
        )

    return _run
