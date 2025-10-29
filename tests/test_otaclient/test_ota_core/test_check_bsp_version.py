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
"""Tests for BSP version compatibility check functionality."""

from __future__ import annotations

from http import HTTPStatus
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from otaclient.boot_control.protocol import BootControllerProtocol
from otaclient.configs._ecu_info import BootloaderType
from otaclient.ota_core import _check_bsp_version
from otaclient.ota_core._check_bsp_version import (
    check_bsp_version_legacy,
)
from otaclient_common.downloader import DownloaderPool

MODULE = _check_bsp_version.__name__


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

        # Mock successful HTTP response
        mock_response = mocker.MagicMock()
        mock_response.status_code = HTTPStatus.OK
        mock_response.text = "# R36 (release), REVISION: 3.0"
        downloader._session.get.return_value = mock_response

        result = _check_bsp_version._download_bsp_version_file(
            "https://example.com/ota", downloader_pool=pool
        )

        assert result == "# R36 (release), REVISION: 3.0"
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

        result = _check_bsp_version._download_bsp_version_file(
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

        result = _check_bsp_version._download_bsp_version_file(
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
        mock_response_success.text = "# R36 (release), REVISION: 3.0"

        downloader._session.get.side_effect = [
            mock_response_fail,
            mock_response_fail,
            mock_response_success,
        ]

        # Mock time.sleep to avoid actual delays
        mocker.patch(f"{MODULE}.time.sleep")

        result = _check_bsp_version._download_bsp_version_file(
            "https://example.com/ota", downloader_pool=pool
        )

        assert result == "# R36 (release), REVISION: 3.0"
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
        mock_response.text = "# R35 (release), REVISION: 4.1"
        downloader._session.get.return_value = mock_response

        result = _check_bsp_version._download_bsp_version_file(
            "https://example.com/ota", downloader_pool=pool
        )

        assert result == "# R35 (release), REVISION: 4.1"
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

        result = _check_bsp_version._download_bsp_version_file(
            "https://example.com/ota", downloader_pool=pool
        )

        # Should return None after retries exhausted
        assert result is None
        pool.release_instance.assert_called_once()


class TestCheckBSPVersionLegacy:
    """Test check_bsp_version_legacy function."""

    @pytest.fixture
    def mock_boot_controller(self, mocker: MockerFixture):
        """Create a mock boot controller."""
        controller = mocker.MagicMock(spec=BootControllerProtocol)
        controller.bootloader_type = BootloaderType.JETSON_UEFI
        controller.check_bsp_version_compatibility.return_value = True
        return controller

    @pytest.fixture
    def mock_downloader_pool(self, mocker: MockerFixture):
        """Create a mock downloader pool."""
        return mocker.MagicMock(spec=DownloaderPool)

    def test_check_bsp_version_legacy_jetson_uefi_compatible(
        self,
        mock_boot_controller: MagicMock,
        mock_downloader_pool: MagicMock,
        mocker: MockerFixture,
    ):
        """Test BSP version check for Jetson UEFI with compatible versions."""
        # Mock download to return BSP version content
        mocker.patch(
            f"{MODULE}._download_bsp_version_file",
            return_value="# R36 (release), REVISION: 3.0",
        )

        result = check_bsp_version_legacy(
            "https://example.com/ota",
            downloader_pool=mock_downloader_pool,
            boot_controller=mock_boot_controller,
        )

        assert result is True
        mock_boot_controller.check_bsp_version_compatibility.assert_called_once_with(
            "# R36 (release), REVISION: 3.0"
        )

    def test_check_bsp_version_legacy_jetson_uefi_incompatible(
        self,
        mock_boot_controller: MagicMock,
        mock_downloader_pool: MagicMock,
        mocker: MockerFixture,
    ):
        """Test BSP version check for Jetson UEFI with incompatible versions."""
        # Mock download to return BSP version content
        mocker.patch(
            f"{MODULE}._download_bsp_version_file",
            return_value="# R36 (release), REVISION: 3.0",
        )
        # Mock incompatibility
        mock_boot_controller.check_bsp_version_compatibility.return_value = False

        result = check_bsp_version_legacy(
            "https://example.com/ota",
            downloader_pool=mock_downloader_pool,
            boot_controller=mock_boot_controller,
        )

        assert result is False
        mock_boot_controller.check_bsp_version_compatibility.assert_called_once()

    def test_check_bsp_version_legacy_file_not_found(
        self,
        mock_boot_controller: MagicMock,
        mock_downloader_pool: MagicMock,
        mocker: MockerFixture,
    ):
        """Test when BSP version file is not found in OTA image."""
        # Mock download to return None (file not found)
        mocker.patch(
            f"{MODULE}._download_bsp_version_file",
            return_value=None,
        )

        result = check_bsp_version_legacy(
            "https://example.com/ota",
            downloader_pool=mock_downloader_pool,
            boot_controller=mock_boot_controller,
        )

        # Should skip check and return True
        assert result is True
        mock_boot_controller.check_bsp_version_compatibility.assert_not_called()

    @pytest.mark.parametrize(
        "bootloader_type",
        [
            BootloaderType.GRUB,
            BootloaderType.RPI_BOOT,
            BootloaderType.JETSON_CBOOT,
        ],
    )
    def test_check_bsp_version_legacy_skipped_for_other_bootloaders(
        self,
        bootloader_type: BootloaderType,
        mock_downloader_pool: MagicMock,
        mocker: MockerFixture,
    ):
        """Test that BSP version check is skipped for other bootloader types."""
        controller = mocker.MagicMock(spec=BootControllerProtocol)
        controller.bootloader_type = bootloader_type

        result = check_bsp_version_legacy(
            "https://example.com/ota",
            downloader_pool=mock_downloader_pool,
            boot_controller=controller,
        )

        assert result is True
        controller.check_bsp_version_compatibility.assert_not_called()
