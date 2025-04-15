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

import json
import threading
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, PropertyMock, mock_open, patch

import pytest
from _otaclient_version import __version__

from otaclient.client_package import (
    Manifest,
    OTAClientPackage,
)
from otaclient.configs.cfg import cfg


class TestClientPackage:
    """Test class for OTAClientPackage"""

    DUMMY_URL = "http://example.com"
    DUMMY_SESSION_DIR = "/tmp/session"
    DUMMY_MANIFEST_URL = "http://example.com/opt/ota/otaclient_release/manifest.json"
    DUMMY_MANIFEST = (
        '{"schema_version": "1", "date": "2025-03-13T00:00:00", '
        '"packages": [{"filename": "package.squashfs", "version": "1.0.0", '
        '"type": "squashfs", "architecture": "x86_64", "size": 123456, '
        '"checksum": "abc123"}]}'
    )

    @pytest.fixture
    def ota_client_package(self):
        # Create mock with the desired structure first
        mock_metadata = MagicMock()
        mock_metadata_jwt = MagicMock()
        # Setup rootfs_directory as a string property that will work with strip()
        mock_metadata_jwt.rootfs_directory = "opt/ota/otaclient_release"

        # Configure the mock to return our structure when metadata_jwt is accessed
        type(mock_metadata).metadata_jwt = PropertyMock(return_value=mock_metadata_jwt)

        ota_client_package = OTAClientPackage(
            base_url=self.DUMMY_URL,
            ota_metadata=mock_metadata,
            session_dir=self.DUMMY_SESSION_DIR,
        )
        return ota_client_package

    @patch(
        "otaclient.client_package.urljoin_ensure_base", return_value=DUMMY_MANIFEST_URL
    )
    @patch("otaclient_common.download_info.DownloadInfo")
    @patch("builtins.open", new_callable=mock_open, read_data=DUMMY_MANIFEST)
    @patch("pathlib.Path.is_file", return_value=True)
    def test_prepare_manifest(
        self,
        mock_is_file,
        mock_open,
        mock_download_info,
        mock_urljoin,
        ota_client_package,
    ):
        condition = MagicMock()
        download_info = list(ota_client_package._prepare_manifest(condition))
        assert len(download_info) == 1

        # Then check that the DownloadInfo was created with the URL from urljoin_ensure_base
        assert download_info[0][0].url == self.DUMMY_MANIFEST_URL

    @patch("builtins.open", new_callable=mock_open, read_data=DUMMY_MANIFEST)
    @patch("pathlib.Path.is_file", return_value=True)
    def test_prepare_manifest_load(self, mock_is_file, mock_open, ota_client_package):
        condition = MagicMock()
        list(ota_client_package._prepare_manifest(condition))
        assert ota_client_package._manifest is not None
        assert isinstance(ota_client_package._manifest, Manifest)

    @patch("otaclient.client_package.urljoin_ensure_base")
    def test_prepare_client_package(self, mock_urljoin, ota_client_package):
        # Configure the mock to return the expected URL
        mock_urljoin.return_value = (
            "http://example.com/opt/ota/otaclient_release/package.squashfs"
        )

        condition = MagicMock()
        manifest_data = json.loads(self.DUMMY_MANIFEST)
        ota_client_package._manifest = Manifest(**manifest_data)
        download_info = list(ota_client_package._prepare_client_package(condition))
        assert len(download_info) == 1
        assert (
            download_info[0][0].url
            == "http://example.com/opt/ota/otaclient_release/package.squashfs"
        )
        assert (
            download_info[0][0].dst
            == ota_client_package._download_dir / "package.squashfs"
        )

    @staticmethod
    def helper_generate_package(
        filename: str,
        package_type: str,
        architecture: str,
        patch_base_version: Optional[str] = None,
    ) -> dict:
        package = {
            "filename": filename,
            "version": "1.0.0",
            "type": package_type,
            "architecture": architecture,
            "size": 123456,
            "checksum": "abc123",
        }
        if patch_base_version:
            package["metadata"] = {"patch_base_version": patch_base_version}
        return package

    @pytest.mark.parametrize(
        "machine, arch, is_squashfs_exists, manifest_data, expected_filename",
        [
            # 0: x86_64 squashfs
            (
                "x86_64",
                "x86_64",
                False,
                {
                    "schema_version": "1",
                    "date": "2025-03-13T00:00:00",
                    "packages": [
                        helper_generate_package(
                            "package_arm64.squashfs", "squashfs", "arm64"
                        ),
                        helper_generate_package(
                            "package_x86_64.squashfs", "squashfs", "x86_64"
                        ),
                    ],
                },
                "package_x86_64.squashfs",
            ),
            # 1: arm64 squashfs
            (
                "aarch64",
                "aarch64",
                False,
                {
                    "schema_version": "1",
                    "date": "2025-03-13T00:00:00",
                    "packages": [
                        helper_generate_package(
                            "package_arm64.squashfs", "squashfs", "arm64"
                        ),
                        helper_generate_package(
                            "package_x86_64.squashfs", "squashfs", "x86_64"
                        ),
                    ],
                },
                "package_arm64.squashfs",
            ),
            # 2: squashfs not exists
            (
                "x86_64",
                "x86_64",
                False,
                {
                    "schema_version": "1",
                    "date": "2025-03-13T00:00:00",
                    "packages": [
                        helper_generate_package(
                            "package_x86_64.squashfs", "squashfs", "x86_64"
                        ),
                        helper_generate_package(
                            "patch_x86_64.squashfs", "patch", "x86_64", __version__
                        ),
                    ],
                },
                "package_x86_64.squashfs",
            ),
            # 3: squashfs exists but version mismatch
            (
                "x86_64",
                "x86_64",
                True,
                {
                    "schema_version": "1",
                    "date": "2025-03-13T00:00:00",
                    "packages": [
                        helper_generate_package(
                            "package_x86_64.squashfs", "squashfs", "x86_64"
                        ),
                        helper_generate_package(
                            "patch_x86_64.squashfs", "patch", "x86_64", "dummy_version"
                        ),
                    ],
                },
                "package_x86_64.squashfs",
            ),
            # 4: squashfs exists and version match
            (
                "x86_64",
                "x86_64",
                True,
                {
                    "schema_version": "1",
                    "date": "2025-03-13T00:00:00",
                    "packages": [
                        helper_generate_package(
                            "package_x86_64.squashfs", "squashfs", "x86_64"
                        ),
                        helper_generate_package(
                            "patch_x86_64.squashfs", "patch", "x86_64", __version__
                        ),
                    ],
                },
                "patch_x86_64.squashfs",
            ),
        ],
    )
    def test_get_available_package_metadata(
        self,
        ota_client_package,
        machine,
        arch,
        is_squashfs_exists,
        manifest_data,
        expected_filename,
    ):
        ota_client_package._manifest = Manifest(**manifest_data)

        with patch("platform.machine", return_value=machine), patch(
            "platform.processor", return_value=arch
        ), patch.object(Path, "is_file", return_value=is_squashfs_exists):
            package = ota_client_package._get_available_package_metadata()
            assert package.filename == expected_filename

    def test_get_target_squashfs_path(self, ota_client_package):
        ota_client_package.package = MagicMock()
        ota_client_package.package.type = "squashfs"
        ota_client_package.downloaded_package_path = Path(
            "/tmp/session/.download/package.squashfs"
        )

        target_path = ota_client_package.get_target_squashfs_path()
        assert target_path == Path("/tmp/session/.download/package.squashfs")

    def test_download_client_package(self, ota_client_package):
        condition = threading.Condition()

        with patch.object(
            ota_client_package, "_prepare_manifest"
        ) as mock_prepare_manifest, patch.object(
            ota_client_package, "_prepare_client_package"
        ) as mock_prepare_client_package:
            mock_prepare_manifest.return_value = iter([[]])
            mock_prepare_client_package.return_value = iter([[]])

            download_info = list(ota_client_package.download_client_package(condition))
            assert len(download_info) == 2

    def test_mount_squashfs(self, ota_client_package):
        ota_client_package.get_target_squashfs_path = MagicMock(
            return_value=Path("/tmp/session/.download/package.squashfs")
        )

        with patch("os.makedirs") as mock_makedirs, patch("subprocess.run") as mock_run:
            mock_makedirs.return_value = None
            mock_run.return_value = None

            ota_client_package.mount_squashfs()
            mock_makedirs.assert_called_once_with(cfg.MOUNT_DIR, exist_ok=True)
            mock_run.assert_called_once_with(
                [
                    "mount",
                    "-t",
                    "squashfs",
                    "/tmp/session/.download/package.squashfs",
                    cfg.MOUNT_DIR,
                ],
                check=True,
            )
