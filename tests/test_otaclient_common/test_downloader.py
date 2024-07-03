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
import threading
import time
from functools import partial
from hashlib import sha256
from multiprocessing import Process
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote

import pytest
import pytest_mock
import zstandard
from requests.structures import CaseInsensitiveDict as CIDict

from otaclient_common.common import urljoin_ensure_base
from otaclient_common.downloader import (
    Downloader,
    DownloaderPool,
    HashVerificationError,
    check_cache_policy_in_resp,
)
from otaclient_common.retry_task_map import ThreadPoolExecutorWithRetry

logger = logging.getLogger(__name__)


TEST_FILES_NUM = 6_000
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

    @pytest.fixture(autouse=True)
    def setup_downloader(self, tmp_path: Path):
        self.downloader = Downloader(hash_func=sha256, chunk_size=4096)
        self._download_dir = tmp_path / "test_download_dir"
        self._download_dir.mkdir(exist_ok=True, parents=True)

    def test_req_inject_cache_control_headers(
        self,
        mocker: pytest_mock.MockerFixture,
        tmp_path: Path,
    ):

        class _ControlledException(Exception):
            """For breakout the actual downloading."""

        # wrap the original get method to capture the call
        mock_get = mocker.MagicMock(
            spec=self.downloader._session.get, side_effect=_ControlledException
        )
        self.downloader._session.get = mock_get
        # patch the downloader to have proxy setting
        mocker.patch.object(
            target=self.downloader,
            attribute="_proxies",
            new={"http": "http://127.0.0.1:8082"},
        )

        file_info = FileInfo(
            file_name="some_file",
            size=123,
            url="http://127.0.0.1/test_url",
            sha256digest="aabccdd1122334455",
            compresson_alg="zstd",
        )

        tmp_file = tmp_path / "a_tmp_file"
        with pytest.raises(_ControlledException):
            self.downloader.download(
                file_info.url,
                tmp_file,
                digest=file_info.sha256digest,
                compression_alg=file_info.compresson_alg,
            )

        # examine the call and ensure the header is injected
        logger.info(f"{mock_get.mock_calls=}")
        mock_get.assert_any_call(
            file_info.url,
            stream=True,
            timeout=mocker.ANY,
            headers={
                "ota-file-cache-control": (
                    f"file_sha256={file_info.sha256digest},"
                    f"file_compression_alg={file_info.compresson_alg}"
                )
            },
        )

    def test_retry_cache_headers_injection(
        self,
        mocker: pytest_mock.MockerFixture,
        setup_test_data: tuple[Path, list[FileInfo]],
        tmp_path: Path,
    ):
        # wrap the original get method to capture the call
        mock_get = mocker.MagicMock(wraps=self.downloader._session.get)
        self.downloader._session.get = mock_get

        _, file_info_list = setup_test_data
        # get one file entry from the list
        file_info = file_info_list[0]

        tmp_file = tmp_path / "a_tmp_file"
        with pytest.raises(HashVerificationError):
            self.downloader.download(file_info.url, tmp_file, digest="wrong_digest!!!")

        # examine the call and ensure the header is injected
        # one normal get call
        logger.info(f"{mock_get.mock_calls=}")
        mock_get.assert_any_call(
            file_info.url,
            stream=True,
            timeout=mocker.ANY,
            headers=None,  # we don't have header pre-set
        )
        # following at least one get call with ota-cache retry header
        mock_get.assert_any_call(
            file_info.url,
            stream=True,
            timeout=mocker.ANY,
            headers={"ota-file-cache-control": "retry_caching"},
        )

    def test_downloading_from_test_data_dir(
        self, setup_test_data: tuple[Path, list[FileInfo]]
    ):
        """Test single thread downloading with one Downloader instance."""
        _, file_info_list = setup_test_data

        logger.info("start to downloading files from test_data_dir")
        for file_info in file_info_list:
            self.downloader.download(
                file_info.url,
                self._download_dir / file_info.file_name,
                digest=file_info.sha256digest,
                size=file_info.size,
                compression_alg=file_info.compresson_alg,
            )
        logger.info("finish downloading files from test_data_dir")


INSTANCE_NUM = 6
MAX_CONCURRENT = 256


class TestDownloaderPool:
    """Test the downloading with downloader pool."""

    @pytest.fixture(autouse=True)
    def setup_test(self, tmp_path: Path):
        self._thread_num = thread_num = INSTANCE_NUM
        self._downloader_pool = DownloaderPool(
            instance_num=thread_num, hash_func=sha256
        )
        self._downloader_mapper: dict[int, Downloader] = {}
        self._download_dir = tmp_path / "test_download_dir"
        self._download_dir.mkdir(exist_ok=True, parents=True)

    def _thread_initializer(self):
        self._downloader_mapper[threading.get_native_id()] = (
            self._downloader_pool.get_instance()
        )

    def _download_file(self, file_info: FileInfo):
        downloader = self._downloader_mapper[threading.get_native_id()]
        downloader.download(
            file_info.url,
            self._download_dir / file_info.file_name,
            digest=file_info.sha256digest,
            size=file_info.size,
            compression_alg=file_info.compresson_alg,
        )

    def test_downloading_from_test_data_dir(
        self,
        setup_test_data: tuple[Path, list[FileInfo]],
    ):
        """Test single thread downloading with one Downloader instance."""
        _, file_info_list = setup_test_data

        logger.info(
            "start to downloading files from test_data_dir with downloader pool"
        )
        with ThreadPoolExecutorWithRetry(
            max_concurrent=MAX_CONCURRENT,
            max_workers=self._thread_num,
            thread_name_prefix="download_ota_files",
            initializer=self._thread_initializer,
        ) as _mapper:
            for _fut in _mapper.ensure_tasks(self._download_file, file_info_list):
                _fut.result()  # expose any possible exception
        logger.info("finish downloading files from test_data_dir")


@pytest.mark.parametrize(
    "_input, _resp_headers, _expected",
    (
        (
            # case1: information from header and image meta are matched.
            ("zstd", "matched_digest"),
            CIDict(
                {
                    "Ota-File-Cache-Control": "file_compression_alg=zstd,file_sha256=matched_digest"
                }
            ),
            ("zstd", "matched_digest"),
        ),
        (
            # case2: digest from header is wrong, raise HashVerificationError.
            ("zstd", "image_meta_digest"),
            CIDict(
                {
                    "Ota-File-Cache-Control": "file_compression_alg=zstd,file_sha256=mismatched_digest"
                }
            ),
            HashVerificationError,
        ),
        (
            # case3: compression_alg mismatched, use the one from header.
            ("mismatched_compression_alg", "matched_digest"),
            CIDict(
                {
                    "Ota-File-Cache-Control": "file_compression_alg=zstd,file_sha256=matched_digest"
                }
            ),
            ("zstd", "matched_digest"),
        ),
        (
            (None, "matched_digest"),
            CIDict(
                {
                    "Ota-File-Cache-Control": "file_compression_alg=zstd,file_sha256=matched_digest"
                }
            ),
            ("zstd", "matched_digest"),
        ),
        (
            # case4: no cache-control header, use the information from image meta as it.
            ("zstd", "input_digest"),
            CIDict(),
            ("zstd", "input_digest"),
        ),
        (
            # case5: image meta doesn't contain digest info(THIS SHOULD NOT HAPPEND NORMALLY).
            #   Not use the digest info from header though.
            (None, None),
            CIDict({"Ota-File-Cache-Control": "file_sha256=not_used"}),
            (None, None),
        ),
    ),
)
def test_check_cache_policy_in_resp(
    _input: tuple[str | None, str | None],
    _resp_headers: CIDict,
    _expected: tuple[str | None, str | None] | type[Exception],
):
    URL = "dummy_url"  # actually only used in logging
    compression_alg, digest = _input

    if isinstance(_expected, type) and issubclass(_expected, Exception):
        with pytest.raises(_expected):
            compression_alg, digest = check_cache_policy_in_resp(
                URL,
                compression_alg=compression_alg,
                digest=digest,
                resp_headers=_resp_headers,
            )
    else:
        digest, compression_alg = check_cache_policy_in_resp(
            URL,
            compression_alg=compression_alg,
            digest=digest,
            resp_headers=_resp_headers,
        )
        assert (compression_alg, digest) == _expected
