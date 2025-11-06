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
"""Tests for BSP version file download functionality."""

from __future__ import annotations

from http import HTTPStatus

import pytest
from pytest_mock import MockerFixture

from otaclient.ota_core import _download_bsp_version_file
from otaclient_common.downloader import DownloaderPool

MODULE = _download_bsp_version_file.__name__


class TestDownloadBSPVersionFile:
    """Test _download_bsp_version_file function."""

    @pytest.fixture
    def mock_downloader_pool(self, mocker: MockerFixture):
        """Create a mock downloader pool."""
        pool = mocker.MagicMock(spec=DownloaderPool)
        downloader = mocker.MagicMock()
        downloader._force_http = False
        downloader._session = mocker.MagicMock()
        pool.get_instance.return_value = downloader
        return pool, downloader

    def test_download_bsp_version_file_success(
        self, mock_downloader_pool: tuple, mocker: MockerFixture
    ):
        """Test successful download of BSP version file."""
        pool, downloader = mock_downloader_pool

        # Mock successful HTTP response with real file format
        mock_response = mocker.MagicMock()
        mock_response.status_code = HTTPStatus.OK
        mock_response.text = """# R36 (release), REVISION: 4.0, GCID: 37537400, BOARD: generic, EABI: aarch64, DATE: Fri Sep 13 04:36:44 UTC 2024
# KERNEL_VARIANT: oot
TARGET_USERSPACE_LIB_DIR=nvidia
TARGET_USERSPACE_LIB_DIR_PATH=usr/lib/aarch64-linux-gnu/nvidia"""
        downloader._session.get.return_value = mock_response

        result = _download_bsp_version_file.download(
            "https://example.com/ota", downloader_pool=pool
        )

        # The strip() is applied in the function, so verify it matches
        assert result == mock_response.text.strip()
        downloader._session.get.assert_called_once()
        pool.release_instance.assert_called_once()

    def test_download_bsp_version_file_not_found(
        self, mock_downloader_pool: tuple, mocker: MockerFixture
    ):
        """Test when BSP version file is not found (404)."""
        pool, downloader = mock_downloader_pool

        # Mock 404 response
        mock_response = mocker.MagicMock()
        mock_response.status_code = HTTPStatus.NOT_FOUND
        downloader._session.get.return_value = mock_response

        result = _download_bsp_version_file.download(
            "https://example.com/ota", downloader_pool=pool
        )

        assert result is None
        pool.release_instance.assert_called_once()

    def test_download_bsp_version_file_unauthorized(
        self, mock_downloader_pool: tuple, mocker: MockerFixture
    ):
        """Test when access is unauthorized (401)."""
        pool, downloader = mock_downloader_pool

        # Mock 401 response
        mock_response = mocker.MagicMock()
        mock_response.status_code = HTTPStatus.UNAUTHORIZED
        downloader._session.get.return_value = mock_response

        result = _download_bsp_version_file.download(
            "https://example.com/ota", downloader_pool=pool
        )

        assert result is None
        pool.release_instance.assert_called_once()

    def test_download_bsp_version_file_with_retry(
        self, mock_downloader_pool: tuple, mocker: MockerFixture
    ):
        """Test retry mechanism on transient failures."""
        pool, downloader = mock_downloader_pool

        # Mock responses: first two fail, third succeeds
        mock_response_fail = mocker.MagicMock()
        mock_response_fail.status_code = HTTPStatus.INTERNAL_SERVER_ERROR

        mock_response_success = mocker.MagicMock()
        mock_response_success.status_code = HTTPStatus.OK
        mock_response_success.text = """# R36 (release), REVISION: 4.0, GCID: 37537400, BOARD: generic, EABI: aarch64, DATE: Fri Sep 13 04:36:44 UTC 2024
# KERNEL_VARIANT: oot
TARGET_USERSPACE_LIB_DIR=nvidia
TARGET_USERSPACE_LIB_DIR_PATH=usr/lib/aarch64-linux-gnu/nvidia"""

        downloader._session.get.side_effect = [
            mock_response_fail,
            mock_response_fail,
            mock_response_success,
        ]

        # Mock time.sleep to avoid actual delays
        mocker.patch(f"{MODULE}.time.sleep")

        result = _download_bsp_version_file.download(
            "https://example.com/ota", downloader_pool=pool
        )

        assert result == mock_response_success.text.strip()
        assert downloader._session.get.call_count == 3
        pool.release_instance.assert_called_once()

    def test_download_bsp_version_file_force_http(
        self, mock_downloader_pool: tuple, mocker: MockerFixture
    ):
        """Test URL conversion when force_http is enabled."""
        pool, downloader = mock_downloader_pool
        downloader._force_http = True

        # Mock successful HTTP response
        mock_response = mocker.MagicMock()
        mock_response.status_code = HTTPStatus.OK
        mock_response.text = """# R35 (release), REVISION: 4.1, GCID: 33958178, BOARD: t186ref, EABI: aarch64, DATE: Tue Aug  1 19:57:35 UTC 2023
# KERNEL_VARIANT: oot
TARGET_USERSPACE_LIB_DIR=nvidia
TARGET_USERSPACE_LIB_DIR_PATH=usr/lib/aarch64-linux-gnu/nvidia"""
        downloader._session.get.return_value = mock_response

        result = _download_bsp_version_file.download(
            "https://example.com/ota", downloader_pool=pool
        )

        assert result == mock_response.text.strip()
        # Verify the URL was converted to http
        called_url = downloader._session.get.call_args[0][0]
        assert called_url.startswith("http://")
        pool.release_instance.assert_called_once()

    def test_download_bsp_version_file_exception(
        self, mock_downloader_pool: tuple, mocker: MockerFixture
    ):
        """Test exception handling during download."""
        pool, downloader = mock_downloader_pool

        # Mock exception
        downloader._session.get.side_effect = Exception("Network error")

        # Mock time.sleep
        mocker.patch(f"{MODULE}.time.sleep")

        result = _download_bsp_version_file.download(
            "https://example.com/ota", downloader_pool=pool
        )

        # Should return None after retries exhausted
        assert result is None
        pool.release_instance.assert_called_once()
