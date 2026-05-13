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
"""Tests for Nvidia Jetson BSP check functionality."""

from __future__ import annotations

import pytest
from pytest_mock import MockerFixture

from otaclient import errors as ota_errors
from otaclient.boot_control._jetson_common import BSPVersion
from otaclient.boot_control._jetson_uefi import JetsonUEFIBootControl
from otaclient.ota_core._updater import OTAUpdaterForLegacyOTAImage
from otaclient_common.downloader import DownloaderPool


class TestNvidiaJetsonCheckBSPLegacy:
    """Test _nvidia_jetson_check_bsp_legacy method."""

    @pytest.fixture
    def mock_ota_updater(self, mocker: MockerFixture):
        """Create a mock OTA updater instance."""
        mock_boot_controller = mocker.MagicMock(spec=JetsonUEFIBootControl)

        updater = mocker.MagicMock(spec=OTAUpdaterForLegacyOTAImage)
        updater._boot_controller = mock_boot_controller
        updater._downloader_pool = mocker.MagicMock(spec=DownloaderPool)
        updater.url_base = "https://example.com/ota"

        # Bind the actual method to the mock instance
        updater._nvidia_jetson_check_bsp_legacy = (
            OTAUpdaterForLegacyOTAImage._nvidia_jetson_check_bsp_legacy.__get__(
                updater, OTAUpdaterForLegacyOTAImage
            )
        )

        return updater, mock_boot_controller

    def test_bsp_version_file_not_found_skips_check(
        self, mock_ota_updater: tuple, mocker: MockerFixture
    ):
        """Test that the check is skipped when BSP version file is not found."""
        updater, mock_boot_controller = mock_ota_updater

        mocker.patch(
            "otaclient.ota_core._download_bsp_version_file.download",
            return_value=None,
        )

        updater._nvidia_jetson_check_bsp_legacy()

        mock_boot_controller.current_slot_bsp_ver_check.assert_not_called()

    def test_bsp_version_check_success(
        self, mock_ota_updater: tuple, mocker: MockerFixture
    ):
        """Test successful BSP version compatibility check."""
        updater, mock_boot_controller = mock_ota_updater

        raw_bsp_content = """# R36 (release), REVISION: 4.0, GCID: 37537400, BOARD: generic, EABI: aarch64, DATE: Fri Sep 13 04:36:44 UTC 2024
# KERNEL_VARIANT: oot
TARGET_USERSPACE_LIB_DIR=nvidia"""
        mocker.patch(
            "otaclient.ota_core._download_bsp_version_file.download",
            return_value=raw_bsp_content,
        )

        parsed_version = BSPVersion(36, 4, 0)
        mocker.patch(
            "otaclient.boot_control._jetson_common.parse_nv_tegra_release",
            return_value=parsed_version,
        )

        mock_boot_controller.current_slot_bsp_ver_check.return_value = (True, "R36.4.0")

        updater._nvidia_jetson_check_bsp_legacy()

        mock_boot_controller.current_slot_bsp_ver_check.assert_called_once_with(
            parsed_version
        )

    def test_bsp_version_check_failure_raises_error(
        self, mock_ota_updater: tuple, mocker: MockerFixture
    ):
        """Test that incompatible BSP versions raise BootControlBSPVersionCompatibilityFailed."""
        updater, mock_boot_controller = mock_ota_updater

        raw_bsp_content = """# R36 (release), REVISION: 4.0, GCID: 37537400, BOARD: generic, EABI: aarch64, DATE: Fri Sep 13 04:36:44 UTC 2024
# KERNEL_VARIANT: oot
TARGET_USERSPACE_LIB_DIR=nvidia"""
        mocker.patch(
            "otaclient.ota_core._download_bsp_version_file.download",
            return_value=raw_bsp_content,
        )

        parsed_version = BSPVersion(36, 4, 0)
        mocker.patch(
            "otaclient.boot_control._jetson_common.parse_nv_tegra_release",
            return_value=parsed_version,
        )

        mock_boot_controller.current_slot_bsp_ver_check.return_value = (
            False,
            "R35.4.1",
        )

        with pytest.raises(
            ota_errors.BootControlBSPVersionCompatibilityFailed
        ) as exc_info:
            updater._nvidia_jetson_check_bsp_legacy()

        assert "doesn't match" in str(exc_info.value)
        assert "R35.4.1" in str(exc_info.value)

    def test_bsp_version_check_with_empty_download_content(
        self, mock_ota_updater: tuple, mocker: MockerFixture
    ):
        """Test that empty download content is handled as file not found."""
        updater, mock_boot_controller = mock_ota_updater

        mocker.patch(
            "otaclient.ota_core._download_bsp_version_file.download",
            return_value="",
        )

        updater._nvidia_jetson_check_bsp_legacy()

        mock_boot_controller.current_slot_bsp_ver_check.assert_not_called()

    def test_integration_with_parse_nv_tegra_release(
        self, mock_ota_updater: tuple, mocker: MockerFixture
    ):
        """Test integration with actual parse_nv_tegra_release function."""
        updater, mock_boot_controller = mock_ota_updater

        raw_bsp_content = """# R35 (release), REVISION: 4.1, GCID: 33958178, BOARD: t186ref, EABI: aarch64, DATE: Tue Aug  1 19:57:35 UTC 2023
# KERNEL_VARIANT: oot
TARGET_USERSPACE_LIB_DIR=nvidia
TARGET_USERSPACE_LIB_DIR_PATH=usr/lib/aarch64-linux-gnu/nvidia"""
        mocker.patch(
            "otaclient.ota_core._download_bsp_version_file.download",
            return_value=raw_bsp_content,
        )

        # Don't mock parse_nv_tegra_release - use the real function
        mock_boot_controller.current_slot_bsp_ver_check.return_value = (True, "R35.4.1")

        updater._nvidia_jetson_check_bsp_legacy()

        expected_version = BSPVersion(35, 4, 1)
        mock_boot_controller.current_slot_bsp_ver_check.assert_called_once_with(
            expected_version
        )
