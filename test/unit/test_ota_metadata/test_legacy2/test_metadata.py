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
"""Tests for OTAMetadata.download_metafiles method."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from ota_metadata.legacy2.metadata import OTAMetadata
from ota_metadata.utils.cert_store import CAChainStore
from otaclient_common.download_info import DownloadInfo

logger = logging.getLogger(__name__)


class TestDownloadMetafiles:
    """Test cases for OTAMetadata.download_metafiles method."""

    @pytest.fixture
    def ota_metadata(self, tmp_path: Path, mocker: MockerFixture) -> OTAMetadata:
        session_dir = tmp_path / "session"
        session_dir.mkdir(parents=True)
        # NOTE: Use `Mock` (not `MagicMock`) so the spec'd `__len__` from Dict
        # isn't auto-configured to return 0; OTAMetadata's constructor checks
        # `if not ca_chains_store:` and would otherwise reject the empty mock.
        ca_store = mocker.Mock(spec=CAChainStore)
        return OTAMetadata(
            base_url="https://example.com/ota",
            session_dir=session_dir,
            ca_chains_store=ca_store,
        )

    def test_download_metafiles_basic_flow(
        self, ota_metadata: OTAMetadata, mocker: MockerFixture
    ):
        """Test basic flow of download_metafiles method."""
        mock_prepare_metadata = mocker.patch.object(ota_metadata, "_prepare_metadata")
        mock_prepare_ota_image_metadata = mocker.patch.object(
            ota_metadata, "_prepare_ota_image_metadata"
        )
        mock_prepare_persist_meta = mocker.patch.object(
            ota_metadata, "_prepare_persist_meta"
        )

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

        condition = threading.Condition()
        downloads = list(ota_metadata.download_metafiles(condition))

        assert len(downloads) == 3
        assert downloads[0] == metadata_downloads
        assert downloads[1] == ota_image_downloads
        assert downloads[2] == persist_downloads

        mock_prepare_metadata.assert_called_once()
        mock_prepare_ota_image_metadata.assert_called_once()
        mock_prepare_persist_meta.assert_called_once()

    def test_download_metafiles_only_metadata_verification(
        self, ota_metadata: OTAMetadata, mocker: MockerFixture
    ):
        """Test download_metafiles with only_metadata_verification=True."""
        mock_prepare_metadata = mocker.patch.object(ota_metadata, "_prepare_metadata")
        mock_prepare_ota_image_metadata = mocker.patch.object(
            ota_metadata, "_prepare_ota_image_metadata"
        )
        mock_prepare_persist_meta = mocker.patch.object(
            ota_metadata, "_prepare_persist_meta"
        )

        metadata_downloads = [
            DownloadInfo(
                url="https://example.com/ota/metadata.jwt",
                dst=Path("metadata.jwt"),
            ),
        ]

        mock_prepare_metadata.return_value = iter([metadata_downloads])

        condition = threading.Condition()
        downloads = list(
            ota_metadata.download_metafiles(condition, only_metadata_verification=True)
        )

        assert len(downloads) == 1
        assert downloads[0] == metadata_downloads

        mock_prepare_metadata.assert_called_once()
        mock_prepare_ota_image_metadata.assert_not_called()
        mock_prepare_persist_meta.assert_not_called()

    def test_download_metafiles_exception_handling(
        self, ota_metadata: OTAMetadata, mocker: MockerFixture
    ):
        """Test exception handling in download_metafiles method."""
        mock_prepare_metadata = mocker.patch.object(ota_metadata, "_prepare_metadata")
        mock_prepare_ota_image_metadata = mocker.patch.object(
            ota_metadata, "_prepare_ota_image_metadata"
        )

        test_exception = Exception("Test exception")
        mock_prepare_metadata.side_effect = test_exception

        condition = threading.Condition()
        generator = ota_metadata.download_metafiles(condition)

        with pytest.raises(Exception) as exc_info:
            list(generator)

        assert exc_info.value is test_exception

        mock_prepare_metadata.assert_called_once()
        mock_prepare_ota_image_metadata.assert_not_called()
