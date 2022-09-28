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


import pytest
import pytest_mock
import requests
import requests_mock
import typing
from pathlib import Path

from otaclient.app.common import file_sha256
from otaclient.app.downloader import (
    ChunkStreamingError,
    ExceedMaxRetryError,
    HashVerificaitonError,
)

from tests.conftest import TestConfiguration as cfg


class TestDownloader:
    # NOTE: full URL is http://<ota_image_url>/metadata.jwt
    #       full path is <ota_image_dir>/metadata.jwt
    TEST_FILE = "metadata.jwt"
    TEST_FILE_PATH = Path(cfg.OTA_IMAGE_DIR) / TEST_FILE
    TEST_FILE_SHA256 = file_sha256(TEST_FILE_PATH)
    TEST_FILE_SIZE = len(TEST_FILE_PATH.read_bytes())

    DOWNLOADER_MODULE_PATH = "app.downloader"

    @pytest.fixture(autouse=True)
    def mock_setup(self, mocker: pytest_mock.MockerFixture):
        _cfg_path = f"{self.DOWNLOADER_MODULE_PATH}.cfg"
        mocker.patch(f"{_cfg_path}.DOWNLOAD_BACKOFF_MAX", 2)

    def test_normal_download(self, tmp_path: Path):
        from otaclient.app.downloader import Downloader

        _downloader = Downloader()
        _target_path = tmp_path / self.TEST_FILE

        _downloader.cleanup_proxy()
        _error = _downloader.download(
            self.TEST_FILE,
            _target_path,
            self.TEST_FILE_SHA256,
            url_base=cfg.OTA_IMAGE_URL,
        )

        assert _error == 0
        assert file_sha256(_target_path) == self.TEST_FILE_SHA256

    def test_download_mismatch_sha256(self, tmp_path: Path):
        from otaclient.app.downloader import Downloader

        _downloader = Downloader()
        _target_path = tmp_path / self.TEST_FILE

        _downloader.cleanup_proxy()
        with pytest.raises(HashVerificaitonError):
            _downloader.download(
                self.TEST_FILE,
                _target_path,
                "wrong_sha256_value",
                url_base=cfg.OTA_IMAGE_URL,
            )

    @pytest.mark.parametrize(
        "inject_requests_err, expected_ota_download_err",
        (
            (requests.exceptions.ConnectTimeout, ExceedMaxRetryError),
            (
                requests.exceptions.HTTPError,
                ExceedMaxRetryError,
            ),  # arbitrary HTTP error
        ),
    )
    def test_download_server_error(
        self,
        tmp_path: Path,
        inject_requests_err,
        expected_ota_download_err,
    ):
        from otaclient.app.downloader import Downloader

        _downloader = Downloader()
        _target_path = tmp_path / self.TEST_FILE

        _mock_adapter = requests_mock.Adapter()
        _mock_adapter.register_uri(
            requests_mock.ANY,
            requests_mock.ANY,
            exc=inject_requests_err,
        )

        # directly load the mocker adapter to the Downloader session
        _downloader.session.mount(cfg.OTA_IMAGE_URL, _mock_adapter)
        with pytest.raises(expected_ota_download_err) as e:
            _downloader.download(
                self.TEST_FILE,
                _target_path,
                self.TEST_FILE_SHA256,
                url_base=cfg.OTA_IMAGE_URL,
            )
        # assert exception catched
        assert isinstance(e.value.__cause__, inject_requests_err)

    def test_raise_streaming_error_on_incompleted_download(self, tmp_path: Path):
        from otaclient.app.downloader import Downloader

        _downloader = Downloader()
        _target_path = tmp_path / self.TEST_FILE

        with pytest.raises(ChunkStreamingError) as e:
            _downloader.download(
                self.TEST_FILE,
                _target_path,
                digest=self.TEST_FILE_SHA256,
                # input wrong size to simulate incomplete download
                size=self.TEST_FILE_SIZE // 2,
                url_base=cfg.OTA_IMAGE_URL,
            )

        assert isinstance(e.value.__cause__, ValueError)

    @pytest.mark.parametrize(
        "inject_requests_err, expected_ota_download_err",
        (
            (requests.exceptions.ChunkedEncodingError, ChunkStreamingError),
            (requests.exceptions.ConnectionError, ChunkStreamingError),
            (requests.exceptions.StreamConsumedError, ChunkStreamingError),
        ),
    )
    def test_download_streaming_error(
        self,
        tmp_path: Path,
        mocker: pytest_mock.MockerFixture,
        inject_requests_err,
        expected_ota_download_err,
    ):
        from otaclient.app.downloader import Downloader

        _downloader = Downloader()
        _target_path = tmp_path / self.TEST_FILE

        # mock the session.get method to return a mock as resp
        _mock_resp = typing.cast(requests.Response, mocker.MagicMock())
        _mock_resp.raw.retries = None
        _mock_resp.iter_content.side_effect = inject_requests_err
        mocker.patch.object(_downloader.session, "get", return_value=_mock_resp)

        with pytest.raises(expected_ota_download_err) as e:
            _downloader.download(
                self.TEST_FILE,
                _target_path,
                self.TEST_FILE_SHA256,
                url_base=cfg.OTA_IMAGE_URL,
            )
        # assert exception catched
        assert isinstance(e.value.__cause__, inject_requests_err)

    def test_retry_headers_injection(
        self, tmp_path: Path, mocker: pytest_mock.MockerFixture
    ):
        from otaclient.app.downloader import Downloader

        _downloader = Downloader()
        _target_path = tmp_path / self.TEST_FILE

        # directly mock the response.iter_content method to return a HashVerificationError
        # for test convenience(it won't happen in the real world!)
        _mock_resp = typing.cast(requests.Response, mocker.MagicMock())
        _mock_resp.raw.retries = None
        _mock_resp.iter_content.side_effect = HashVerificaitonError(
            url="", dst=""
        )  # NOTE: url and dst are not important in this test
        _mock_get = mocker.MagicMock(return_value=_mock_resp)
        mocker.patch.object(_downloader.session, "get", _mock_get)

        with pytest.raises(HashVerificaitonError):
            _downloader.download(
                self.TEST_FILE,
                _target_path,
                self.TEST_FILE_SHA256,
                url_base=cfg.OTA_IMAGE_URL,
            )

        # one normal get call
        _mock_get.assert_any_call(
            f"{cfg.OTA_IMAGE_URL}/{self.TEST_FILE}",
            stream=True,
            cookies={},
            headers={},
        )
        # following at least one get call with ota-cache retry header
        _mock_get.assert_any_call(
            f"{cfg.OTA_IMAGE_URL}/{self.TEST_FILE}",
            stream=True,
            cookies={},
            headers={"ota-file-cache-control": "retry_caching"},
        )
