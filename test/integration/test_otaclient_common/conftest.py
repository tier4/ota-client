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
"""Shared fixtures for the otaclient_common integration suite.

The downloader integration tests need a real HTTP server to talk to.
This conftest bundles:
  * ``setup_test_data`` — generate a directory of urandom blobs (some
    zstd-compressed) and surface their canonical metadata
  * ``run_http_server_subprocess`` — host that directory via a daemon
    HTTP subprocess on an ephemeral port (autouse)

Both fixtures are module-scoped: the cost of generating thousands of
random blobs and starting a process is amortised across every test in
the module.
"""

from __future__ import annotations

import itertools
import logging
import os
import random
import socket
import time
from functools import partial
from hashlib import sha256
from multiprocessing import Process
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote

import pytest
import zstandard

from otaclient_common.common import urljoin_ensure_base

logger = logging.getLogger(__name__)

# A modest default keeps the suite quick on CI while still exercising
# the threaded download pool.  Individual tests can opt to use a slice.
TEST_FILES_NUM = 256
# NOTE(20240702): exercise URL escaping
TEST_SPECIAL_FILE_NAME = r"path;adf.ae?qu.er\y=str#fragファイルement"
TEST_FILE_SIZE_LOWER_BOUND = 128
TEST_FILE_SIZE_UPPER_BOUND = 4096
COMPRESSION_FILE_SIZE_LOWER_BOUND = 1024


class FileInfo(NamedTuple):
    file_name: str
    url: str
    size: int
    sha256digest: str
    compresson_alg: str | None


def _find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def http_server_endpoint() -> tuple[str, int]:
    """Allocate a free port for the test HTTP server.

    Each test module gets its own port to avoid collisions when the
    runner re-imports modules for parametrized collections.
    """
    return "127.0.0.1", _find_free_port()


@pytest.fixture(scope="module")
def setup_test_data(
    tmp_path_factory: pytest.TempPathFactory,
    http_server_endpoint: tuple[str, int],
) -> tuple[Path, list[FileInfo]]:
    """Generate the on-disk corpus the test HTTP server will serve.

    Each blob:
      * has random content of length [128, 4096) bytes,
      * is zstd-compressed if size >= 1KiB,
      * is named by its index (with one extra blob using
        TEST_SPECIAL_FILE_NAME to exercise URL escaping).
    The returned ``FileInfo`` records hold the *uncompressed* size and
    digest, mirroring how OTA image metadata describes the resource.
    """
    host, port = http_server_endpoint
    test_data_dir = tmp_path_factory.mktemp("downloader_test_data")
    zstd_cctx = zstandard.ZstdCompressor()
    base_url = f"http://{host}:{port}/"

    file_info_list: list[FileInfo] = []
    for fname in map(
        str, itertools.chain(range(TEST_FILES_NUM), [TEST_SPECIAL_FILE_NAME])
    ):
        file = test_data_dir / fname
        # see legacy parser for URL-escape behaviour:
        #   tests have backwards-compat coverage for the old OTA image format
        file_url = urljoin_ensure_base(base_url, quote(fname))

        file_size = random.randint(
            TEST_FILE_SIZE_LOWER_BOUND, TEST_FILE_SIZE_UPPER_BOUND
        )
        file_content = os.urandom(file_size)
        # size and sha256 reflect the uncompressed payload, not the
        # bytes actually stored on disk
        file_sha256digest = sha256(file_content).hexdigest()

        zstd_compressed = file_size >= COMPRESSION_FILE_SIZE_LOWER_BOUND
        if zstd_compressed:
            file_content = zstd_cctx.compress(file_content)

        file.write_bytes(file_content)
        file_info_list.append(
            FileInfo(
                file_name=str(fname),
                url=file_url,
                size=file_size,
                sha256digest=file_sha256digest,
                compresson_alg="zstd" if zstd_compressed else None,
            )
        )
    os.sync()

    random.shuffle(file_info_list)
    logger.info("finished generating %d test blobs", len(file_info_list))
    return test_data_dir, file_info_list


@pytest.fixture(autouse=True, scope="module")
def run_http_server_subprocess(
    setup_test_data: tuple[Path, list[FileInfo]],
    http_server_endpoint: tuple[str, int],
):
    """Serve the generated corpus over HTTP for the lifetime of the module."""
    test_data_dir, _ = setup_test_data
    host, port = http_server_endpoint

    def _run():
        import http.server as http_server

        def _silent_logger(*args, **kwargs):
            """Mute the SimpleHTTPRequestHandler default access log."""

        http_server.SimpleHTTPRequestHandler.log_message = _silent_logger

        handler_class = partial(
            http_server.SimpleHTTPRequestHandler,
            directory=str(test_data_dir),
        )
        with http_server.ThreadingHTTPServer((host, port), handler_class) as httpd:
            httpd.serve_forever()

    server_p = Process(target=_run, daemon=True)
    try:
        server_p.start()
        # wait for the server to bind; poll the port instead of a fixed sleep
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((host, port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError(f"test HTTP server did not come up on {host}:{port}")
        logger.info("test HTTP server up on %s:%s for %s", host, port, test_data_dir)
        yield
    finally:
        logger.info("shutting down test HTTP server")
        server_p.kill()
        server_p.join(timeout=5)
