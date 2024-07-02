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

import itertools
import logging
import os
import random
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
from otaclient_common.downloader import Downloader

logger = logging.getLogger(__name__)


TEST_FILES_NUM = 2_000
# NOTE(20240702): This is for URL escape testing
TEST_SPECIAL_FILE_NAME = r"path;adf.ae?qu.er\y=str#fragファイルement"
TEST_FILE_SIZE_LOWER_BOUND = 128
TEST_FILE_SIZE_UPPER_BOUND = 4096  # 4KiB
# enable zstd compression on file larger than 1KiB
COMPRESSION_FILE_SIZE_LOWER_BOUND = 1024
TEST_HTTP_SERVER_IP = "127.0.0.1"
TEST_HTTP_SERVER_PORT = 8889


class FileInfo(NamedTuple):
    file_name: str
    url: str
    size: int
    sha256digest: str
    compresson_alg: str | None


@pytest.fixture(scope="module")
def setup_test_data(
    tmp_path_factory: pytest.TempPathFactory,
    *,
    host: str = TEST_HTTP_SERVER_IP,
    port: int = TEST_HTTP_SERVER_PORT,
) -> tuple[Path, list[FileInfo]]:
    """Download test files generating.

    The test files has the following properties:
    1. file name is the index,
    2. file content is urandom with random length from 128 to 4096 bytes.
    3. a special file with file name TEST_SPECIAL_FILE_NAME will be added.
    """
    test_data_dir = tmp_path_factory.mktemp("test_data_dir")
    zstd_cctx = zstandard.ZstdCompressor()
    base_url = f"http://{host}:{port}/"

    file_info_list: list[FileInfo] = []
    for fname in map(
        str, itertools.chain(range(TEST_FILES_NUM), [TEST_SPECIAL_FILE_NAME])
    ):
        file = test_data_dir / fname

        # for how the URL is escaped, see
        #   https://github.com/tier4/ota-client/blob/a19f92ad4c66e3039101bdb8b83f85fc687eb32b/src/ota_metadata/legacy/parser.py#L779-L787
        #   for more details.
        #   This is for backward compatible with the old OTA image format.
        file_url = urljoin_ensure_base(base_url, quote(fname))

        file_size = random.randint(
            TEST_FILE_SIZE_LOWER_BOUND,
            TEST_FILE_SIZE_UPPER_BOUND,
        )
        file_content = os.urandom(file_size)
        # NOTE that the file_size and the file_sha256digest are the original plain
        #   file's one, not the compressed file's one.
        file_sha256digest = sha256(file_content).hexdigest()

        zstd_compressed = file_size >= COMPRESSION_FILE_SIZE_LOWER_BOUND
        if zstd_compressed:
            file_content = zstd_cctx.compress(file_content)

        file.write_bytes(file_content)
        file_info = FileInfo(
            file_name=str(fname),
            url=file_url,
            size=file_size,
            sha256digest=file_sha256digest,
            compresson_alg="zstd" if zstd_compressed else None,
        )
        file_info_list.append(file_info)
    os.sync()

    random.shuffle(file_info_list)
    logger.info("finish up generating test_data_dir")
    return test_data_dir, file_info_list


@pytest.fixture(autouse=True, scope="module")
def run_http_server_subprocess(
    setup_test_data: tuple[Path, list[FileInfo]],
    *,
    host: str = TEST_HTTP_SERVER_IP,
    port: int = TEST_HTTP_SERVER_PORT,
):
    """Launch a HTTP server to host the test_data_dir."""
    test_data_dir, _ = setup_test_data

    def run_http_server():
        import http.server as http_server

        def _dummy_logger(*args, **kwargs):
            """This is for muting the logging of the HTTP request."""

        http_server.SimpleHTTPRequestHandler.log_message = _dummy_logger

        handler_class = partial(
            http_server.SimpleHTTPRequestHandler,
            directory=str(test_data_dir),
        )
        with http_server.ThreadingHTTPServer((host, port), handler_class) as httpd:
            httpd.serve_forever()

    _server_p = Process(target=run_http_server, daemon=True)
    try:
        _server_p.start()
        # NOTE: wait for 3 seconds for the server to fully start
        time.sleep(3)
        logger.info(f"start background file server on {test_data_dir}")
        yield
    finally:
        logger.info("shutdown background ota-image server")
        _server_p.kill()


class TestDownloader:

    def test_downloading(
        self,
        setup_test_data: tuple[Path, list[FileInfo]],
        tmp_path: Path,
    ):
        """Test single thread downloading with one Downloader instance."""
        downloader_inst = Downloader(hash_func=sha256, chunk_size=4096)
        _, file_info_list = setup_test_data

        buffer_area = tmp_path / "_buffer_area"
        logger.info("start to downloading files from test_data_dir")
        for file_info in file_info_list:
            try:
                downloader_inst.download(
                    file_info.url,
                    buffer_area,
                    digest=file_info.sha256digest,
                    size=file_info.size,
                    compression_alg=file_info.compresson_alg,
                )
            except Exception as e:
                logger.error(f"{e!r}")


class TestDownloaderPool:
    pass
