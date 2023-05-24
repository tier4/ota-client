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


import asyncio
import logging
import threading
import pytest
import pytest_mock
import requests
import requests_mock
from pathlib import Path
from urllib.parse import urlsplit, urljoin


from otaclient.app.common import file_sha256, urljoin_ensure_base
from otaclient.app.downloader import (
    Downloader,
    DestinationNotAvailableError,
    ChunkStreamingError,
    ExceedMaxRetryError,
    HashVerificaitonError,
    UnhandledHTTPError,
)

from tests.conftest import TestConfiguration as cfg
from tests.utils import zstd_compress_file


logger = logging.getLogger(__name__)


class _SimpleDummyApp:
    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["method"] != "GET":
            return

        url = urlsplit(scope["path"])
        _input_status_code = url.path.strip("/")
        # send a request with the http code indicated by URL path
        await send(
            {
                "type": "http.response.start",
                "status": int(_input_status_code),
                "headers": [
                    [b"content-type", b"text/html;charset=UTF-8"],
                ],
            }
        )
        await send({"type": "http.response.body", "body": _input_status_code.encode()})


@pytest.fixture
def launch_dummy_server(host: str = "127.0.0.1", port: int = 9999):
    _should_exit = threading.Event()

    async def _launcher():
        import uvicorn

        _config = uvicorn.Config(
            _SimpleDummyApp(),
            host=host,
            port=port,
        )
        server = uvicorn.Server(_config)
        config = server.config
        if not config.loaded:
            config.load()
        server.lifespan = config.lifespan_class(config)
        await server.startup()
        logger.info("dummy server started")
        while True:
            if _should_exit.is_set():
                await server.shutdown()
                break
            await asyncio.sleep(2)

    try:
        _t = threading.Thread(target=asyncio.run, args=(_launcher(),))
        _t.start()
        yield
    finally:
        logger.info("dummy server shutdown")
        _should_exit.set()
        _t.join()


class TestDownloader:
    # NOTE: full URL is http://<ota_image_url>/metadata.jwt
    #       full path is <ota_image_dir>/metadata.jwt
    TEST_FILE = "metadata.jwt"
    TEST_FILE_PATH = Path(cfg.OTA_IMAGE_DIR) / TEST_FILE
    TEST_FILE_SHA256 = file_sha256(TEST_FILE_PATH)
    TEST_FILE_SIZE = len(TEST_FILE_PATH.read_bytes())

    @pytest.fixture
    def prepare_zstd_compressed_files(self):
        # prepare a compressed file under OTA image dir,
        # and then remove it after test finished
        try:
            self.zstd_compressed = Path(cfg.OTA_IMAGE_DIR) / f"{self.TEST_FILE}.zst"
            zstd_compress_file(self.TEST_FILE_PATH, self.zstd_compressed)
            yield
        finally:
            self.zstd_compressed.unlink(missing_ok=True)

    @pytest.fixture(autouse=True)
    def launch_downloader(self, mocker: pytest_mock.MockerFixture):
        self.session = requests.Session()
        mocker.patch("requests.Session", return_value=self.session)
        mocker.patch.object(Downloader, "BACKOFF_MAX", 0.1)
        mocker.patch.object(Downloader, "RETRY_COUNT", 3)
        try:
            self.downloader = Downloader()
            yield
        finally:
            self.downloader.shutdown()

    def test_normal_download(self, tmp_path: Path):
        _target_path = tmp_path / self.TEST_FILE

        url = urljoin_ensure_base(cfg.OTA_IMAGE_URL, self.TEST_FILE)
        _error, _read_download_size, _ = self.downloader.download(
            url,
            _target_path,
            digest=self.TEST_FILE_SHA256,
            size=self.TEST_FILE_SIZE,
        )

        assert _error == 0
        assert _read_download_size == self.TEST_FILE_PATH.stat().st_size
        assert file_sha256(_target_path) == self.TEST_FILE_SHA256

    def test_download_zstd_compressed_file(
        self, tmp_path: Path, prepare_zstd_compressed_files
    ):
        _target_path = tmp_path / self.TEST_FILE

        url = urljoin_ensure_base(cfg.OTA_IMAGE_URL, f"{self.TEST_FILE}.zst")
        # first test directly download without decompression
        _error, _read_download_bytes_a, _ = self.downloader.download(url, _target_path)
        assert _error == 0
        assert file_sha256(_target_path) == file_sha256(self.zstd_compressed)

        # second, test dwonloader with transparent zstd decompression
        _error, _real_download_bytes_b, _ = self.downloader.download(
            url,
            _target_path,
            digest=self.TEST_FILE_SHA256,
            size=self.TEST_FILE_SIZE,
            compression_alg="zst",
        )
        assert _error == 0
        # downloader reports the real downloaded bytes num
        assert (
            _read_download_bytes_a
            == _real_download_bytes_b
            == self.zstd_compressed.stat().st_size
        )
        assert file_sha256(_target_path) == self.TEST_FILE_SHA256

    def test_download_mismatch_sha256(self, tmp_path: Path):
        _target_path = tmp_path / self.TEST_FILE

        url = urljoin_ensure_base(cfg.OTA_IMAGE_URL, self.TEST_FILE)
        with pytest.raises(HashVerificaitonError):
            self.downloader.download(
                url,
                _target_path,
                digest="wrong_sha256hash",
                size=self.TEST_FILE_SIZE,
            )

    @pytest.mark.parametrize(
        "inject_requests_err, expected_ota_download_err",
        (
            (requests.exceptions.ChunkedEncodingError, ChunkStreamingError),
            (requests.exceptions.ConnectionError, ChunkStreamingError),
            (requests.exceptions.HTTPError, UnhandledHTTPError),
            (FileNotFoundError, DestinationNotAvailableError),
            (requests.exceptions.RequestException, ExceedMaxRetryError),
        ),
    )
    def test_download_errors_handling(
        self,
        tmp_path: Path,
        inject_requests_err,
        expected_ota_download_err,
        mocker: pytest_mock.MockerFixture,
    ):
        _mock_adapter = requests_mock.Adapter()
        _mock_adapter.register_uri(
            requests_mock.ANY,
            requests_mock.ANY,
            exc=inject_requests_err,
        )

        # load the mocker adapter to the Downloader session
        self.session.mount(cfg.OTA_IMAGE_URL, _mock_adapter)

        _target_path = tmp_path / self.TEST_FILE
        url = urljoin_ensure_base(cfg.OTA_IMAGE_URL, self.TEST_FILE)
        with pytest.raises(expected_ota_download_err):
            self.downloader.download(
                url,
                _target_path,
                size=self.TEST_FILE_SIZE,
                digest=self.TEST_FILE_SHA256,
            )

    @pytest.mark.parametrize(
        "status_code, expected_ota_download_err",
        (
            # handled by urllib3.Retry
            (413, ExceedMaxRetryError),
            (429, ExceedMaxRetryError),
            (500, ExceedMaxRetryError),
            (502, ExceedMaxRetryError),
            (503, ExceedMaxRetryError),
            (504, ExceedMaxRetryError),
            # target file unavailabe
            (403, UnhandledHTTPError),
            (404, UnhandledHTTPError),
        ),
    )
    def test_download_server_with_http_error(
        self,
        tmp_path: Path,
        status_code,
        expected_ota_download_err,
        launch_dummy_server,
    ):
        url = urljoin("http://127.0.0.1:9999/", str(status_code))
        _target_path = tmp_path / self.TEST_FILE
        with pytest.raises(expected_ota_download_err):
            self.downloader.download(
                url,
                _target_path,
                size=self.TEST_FILE_SIZE,
                digest=self.TEST_FILE_SHA256,
            )

    def test_retry_headers_injection(
        self, tmp_path: Path, mocker: pytest_mock.MockerFixture
    ):
        _mock_get = mocker.MagicMock(wraps=self.session.get)
        self.session.get = _mock_get

        _target_path = tmp_path / self.TEST_FILE
        url = urljoin_ensure_base(cfg.OTA_IMAGE_URL, self.TEST_FILE)
        with pytest.raises(HashVerificaitonError):
            self.downloader.download(url, _target_path, digest="wrong_digest")

        # one normal get call
        logger.error(f"{_mock_get.mock_calls=}")
        _mock_get.assert_any_call(
            url,
            stream=True,
            proxies={},  # the proxies and cookies are regulated by download
            cookies={},
            headers=None,
        )
        # # following at least one get call with ota-cache retry header
        _mock_get.assert_any_call(
            url,
            stream=True,
            proxies={},
            cookies={},
            headers={"ota-file-cache-control": "retry_caching"},
        )
