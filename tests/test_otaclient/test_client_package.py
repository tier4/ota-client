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
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, mock_open, patch

import pytest

from _otaclient_version import __version__
from otaclient.client_package import (
    Manifest,
    OTAClientPackage,
    ReleasePackage,
)


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
        return OTAClientPackage(
            base_url=self.DUMMY_URL, session_dir=self.DUMMY_SESSION_DIR
        )

    @patch(
        "otaclient_common.common.urljoin_ensure_base", return_value=DUMMY_MANIFEST_URL
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
        assert download_info[0][0].url == self.DUMMY_MANIFEST_URL
        assert (
            download_info[0][0].dst
            == ota_client_package._download_dir / "manifest.json"
        )

    @patch("builtins.open", new_callable=mock_open, read_data=DUMMY_MANIFEST)
    @patch("pathlib.Path.is_file", return_value=True)
    def test_prepare_manifest_load(self, mock_is_file, mock_open, ota_client_package):
        condition = MagicMock()
        list(ota_client_package._prepare_manifest(condition))
        assert ota_client_package._manifest is not None
        assert isinstance(ota_client_package._manifest, Manifest)

    def test_prepare_client_package(self, ota_client_package):
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

    @patch("subprocess.run")
    @patch("builtins.open", new_callable=mock_open)
    def test_run_service(self, mock_open, mock_subprocess, ota_client_package):
        manifest_data = json.loads(self.DUMMY_MANIFEST)
        package_data = manifest_data["packages"][0]

        ota_client_package.package = ReleasePackage(**package_data)
        ota_client_package.downloaded_package_file = Path("/tmp/package.squashfs")
        ota_client_package.current_squashfs_path = Path("/tmp/current.squashfs")
        ota_client_package.run_service()
        mock_subprocess.assert_called()

    @patch("subprocess.run")
    def test_finalize(self, mock_subprocess, ota_client_package):
        ota_client_package.finalize()
        mock_subprocess.assert_called()
