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
"""
Tests for OTAMetadata.download_metafiles method.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest
from pytest_mock import MockerFixture

from ota_metadata.legacy2.metadata import OTAMetadata
from ota_metadata.utils.cert_store import CAChainStore
from otaclient_common.download_info import DownloadInfo

logger = logging.getLogger(__name__)


class TestDownloadMetafiles:
    """Test cases for OTAMetadata.download_metafiles method."""

    def setup_method(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.session_dir = Path(self.temp_dir) / "session"
        self.session_dir.mkdir(parents=True)

        # Mock CA store
        self.ca_store = Mock(spec=CAChainStore)

        # Create test OTAMetadata instance
        self.ota_metadata = OTAMetadata(
            base_url="https://example.com/ota",
            session_dir=self.session_dir,
            ca_chains_store=self.ca_store,
        )

    def teardown_method(self):
        """Clean up test fixtures."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_download_metafiles_basic_flow(self, mocker: MockerFixture):
        """Test basic flow of download_metafiles method."""
        # Mock the internal helper methods
        mock_prepare_metadata = mocker.patch.object(
            self.ota_metadata, "_prepare_metadata"
        )
        mock_prepare_ota_image_metadata = mocker.patch.object(
            self.ota_metadata, "_prepare_ota_image_metadata"
        )
        mock_prepare_persist_meta = mocker.patch.object(
            self.ota_metadata, "_prepare_persist_meta"
        )

        # Set up mock return values
        metadata_downloads = [
            DownloadInfo(
                url="https://example.com/ota/metadata.jwt",
                dst=Path("metadata.jwt"),
            ),
            DownloadInfo(
                url="https://example.com/ota/certificate.pem",
                dst=Path("certificate.pem"),
                digest_alg="sha256",
                digest="abc123",
            ),
        ]

        ota_image_downloads = [
            DownloadInfo(
                url="https://example.com/ota/regular",
                dst=Path("regular"),
                digest_alg="sha256",
                digest="def456",
            ),
            DownloadInfo(
                url="https://example.com/ota/directory",
                dst=Path("directory"),
                digest_alg="sha256",
                digest="ghi789",
            ),
        ]

        persist_downloads = [
            DownloadInfo(
                url="https://example.com/ota/persistent",
                dst=Path("persistent"),
                digest_alg="sha256",
                digest="jkl012",
            ),
        ]

        mock_prepare_metadata.return_value = iter([metadata_downloads])
        mock_prepare_ota_image_metadata.return_value = iter([ota_image_downloads])
        mock_prepare_persist_meta.return_value = iter([persist_downloads])

        # Execute the method
        condition = threading.Condition()
        generator = self.ota_metadata.download_metafiles(condition)

        # Collect all yielded download info lists
        downloads = list(generator)

        # Verify the results
        assert len(downloads) == 3
        assert downloads[0] == metadata_downloads
        assert downloads[1] == ota_image_downloads
        assert downloads[2] == persist_downloads

        # Verify method calls
        mock_prepare_metadata.assert_called_once()
        mock_prepare_ota_image_metadata.assert_called_once()
        mock_prepare_persist_meta.assert_called_once()

    def test_download_metafiles_only_metadata_verification(self, mocker: MockerFixture):
        """Test download_metafiles with only_metadata_verification=True."""
        # Mock the internal helper methods
        mock_prepare_metadata = mocker.patch.object(
            self.ota_metadata, "_prepare_metadata"
        )
        mock_prepare_ota_image_metadata = mocker.patch.object(
            self.ota_metadata, "_prepare_ota_image_metadata"
        )
        mock_prepare_persist_meta = mocker.patch.object(
            self.ota_metadata, "_prepare_persist_meta"
        )

        # Set up mock return values
        metadata_downloads = [
            DownloadInfo(
                url="https://example.com/ota/metadata.jwt",
                dst=Path("metadata.jwt"),
            ),
        ]

        mock_prepare_metadata.return_value = iter([metadata_downloads])

        # Execute the method with only_metadata_verification=True
        condition = threading.Condition()
        generator = self.ota_metadata.download_metafiles(
            condition, only_metadata_verification=True
        )

        # Collect all yielded download info lists
        downloads = list(generator)

        # Verify the results
        assert len(downloads) == 1
        assert downloads[0] == metadata_downloads

        # Verify method calls
        mock_prepare_metadata.assert_called_once()
        mock_prepare_ota_image_metadata.assert_not_called()
        mock_prepare_persist_meta.assert_not_called()

    def test_download_metafiles_exception_handling(self, mocker: MockerFixture):
        """Test exception handling in download_metafiles method."""
        # Mock the internal helper methods
        mock_prepare_metadata = mocker.patch.object(
            self.ota_metadata, "_prepare_metadata"
        )
        mock_prepare_ota_image_metadata = mocker.patch.object(
            self.ota_metadata, "_prepare_ota_image_metadata"
        )

        # Set up mock to raise exception
        test_exception = Exception("Test exception")
        mock_prepare_metadata.side_effect = test_exception

        # Execute the method and expect exception
        condition = threading.Condition()
        generator = self.ota_metadata.download_metafiles(condition)

        with pytest.raises(Exception) as exc_info:
            list(generator)

        # Verify the exception is propagated
        assert exc_info.value is test_exception

        # Verify method calls
        mock_prepare_metadata.assert_called_once()
        mock_prepare_ota_image_metadata.assert_not_called()
