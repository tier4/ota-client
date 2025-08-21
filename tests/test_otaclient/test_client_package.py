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
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from _otaclient_version import __version__

from otaclient.client_package import (
    Manifest,
    OTAClientPackageDownloader,
)
from tests.conftest import TEST_DIR


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


class TestClientPackageDownloader:
    """Test class for OTAClientPackageDownloader"""

    DUMMY_URL = "http://example.com"
    DUMMY_SESSION_DIR = "/tmp/session"
    DUMMY_PACKAGE_INSTALL_DIR = "/dummy/otaclient_release"
    DUMMY_DYNAMIC_CLIENT_SQUASHFS_FILE = "/dummy/package.squashfs"
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

        ota_client_package = OTAClientPackageDownloader(
            base_url=self.DUMMY_URL,
            ota_metadata=mock_metadata,
            session_dir=self.DUMMY_SESSION_DIR,
            package_install_dir=self.DUMMY_PACKAGE_INSTALL_DIR,
            squashfs_file=self.DUMMY_DYNAMIC_CLIENT_SQUASHFS_FILE,
        )
        return ota_client_package

    @patch(
        "otaclient.client_package.urljoin_ensure_base", return_value=DUMMY_MANIFEST_URL
    )
    @patch("otaclient_common.download_info.DownloadInfo")
    @patch("pathlib.Path.read_text", return_value=DUMMY_MANIFEST)
    def test_prepare_manifest(
        self,
        mock_url_join,
        mock_download_info,
        mock_read_text,
        ota_client_package: OTAClientPackageDownloader,
    ):
        condition = MagicMock()
        download_info = list(ota_client_package._prepare_manifest(condition))
        assert len(download_info) == 1

        # Then check that the DownloadInfo was created with the URL from urljoin_ensure_base
        assert download_info[0][0].url == self.DUMMY_MANIFEST_URL

    @patch("pathlib.Path.read_text", return_value=DUMMY_MANIFEST)
    def test_prepare_manifest_load(
        self, mock_read_text, ota_client_package: OTAClientPackageDownloader
    ):
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

    @pytest.mark.parametrize(
        "machine, arch, is_squashfs_exists, is_zstd_supported, manifest_data, expected_filename",
        [
            # 0: x86_64 squashfs
            (
                "x86_64",
                "x86_64",
                False,
                True,
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
                True,
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
                "package_x86_64.squashfs",
            ),
            # 3: squashfs exists but version mismatch
            (
                "x86_64",
                "x86_64",
                True,
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
            # 4: squashfs exists and version match, but no zstd support
            (
                "x86_64",
                "x86_64",
                True,
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
        ],
    )
    def test_get_available_package_metadata(
        self,
        ota_client_package,
        machine,
        arch,
        is_squashfs_exists,
        is_zstd_supported,
        manifest_data,
        expected_filename,
    ):
        ota_client_package._manifest = Manifest(**manifest_data)

        with patch("platform.machine", return_value=machine), patch(
            "platform.processor", return_value=arch
        ), patch.object(Path, "is_file", return_value=is_squashfs_exists), patch(
            "otaclient.client_package.shutil.which",
            return_value="/fake/path/to/zstd" if is_zstd_supported else None,
        ):
            package = ota_client_package._get_available_package_metadata()
            assert package.filename == expected_filename

    @pytest.mark.parametrize(
        "package_type, expected_path",
        [
            ("squashfs", "/tmp/session/.download/package.squashfs"),
            ("patch", "/tmp/session/package.squashfs"),
        ],
    )
    def test_get_target_squashfs_path(
        self, ota_client_package, package_type, expected_path
    ):
        ota_client_package.package = MagicMock()
        ota_client_package.package.type = package_type
        ota_client_package._session_dir = Path("/tmp/session")
        ota_client_package.downloaded_package_path = (
            ota_client_package._session_dir / ".download/package.squashfs"
        )
        ota_client_package._create_squashfs_from_patch = MagicMock()

        target_path = ota_client_package._get_target_squashfs_path()

        assert target_path == Path(expected_path)

    def test_create_squashfs_from_patch(self, ota_client_package):
        if not shutil.which("zstd"):
            pytest.skip("zstd is not available, skipping patch test")

        TEST_DATA_DIR = TEST_DIR / "data" / "client_package"

        # Create temporary paths for testing
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            # Copy the test files to the temp directory to ensure path consistency
            test_squashfs = temp_dir_path / "v1.squashfs"
            test_patch = temp_dir_path / "v1_v2.patch"
            shutil.copy(TEST_DATA_DIR / "v1.squashfs", test_squashfs)
            shutil.copy(TEST_DATA_DIR / "v1_v2.patch", test_patch)

            ota_client_package.package = MagicMock()
            ota_client_package.package.architecture = "x86_64"
            ota_client_package.package.type = "patch"
            ota_client_package.package.version = "1.2.3"
            ota_client_package._session_dir = temp_dir_path
            ota_client_package.downloaded_package_path = test_patch
            ota_client_package.current_squashfs_path = test_squashfs

            target_squashfs_path = temp_dir_path / "tmp.squashfs"

            # verify the target squashfs file doesn't exist
            assert not target_squashfs_path.exists()

            # call the target method and apply the patch to the input squashfs
            ota_client_package._create_squashfs_from_patch(target_squashfs_path)

            # verify the target squashfs file is created
            assert target_squashfs_path.exists()

    @pytest.mark.parametrize(
        "is_same_package_version, expected_download_info_length",
        [
            (True, 1),  # Same version, only manifest
            (False, 2),  # Different version, need to download both
        ],
    )
    def test_download_client_package(
        self, ota_client_package, is_same_package_version, expected_download_info_length
    ):
        condition = threading.Condition()

        # Test when client version is different (needs download)
        with patch.object(
            ota_client_package, "_prepare_manifest"
        ) as mock_prepare_manifest, patch.object(
            ota_client_package, "_prepare_client_package"
        ) as mock_prepare_client_package, patch.object(
            ota_client_package, "is_same_client_package_version"
        ) as mock_is_same_version:
            mock_prepare_manifest.return_value = iter([[]])
            mock_prepare_client_package.return_value = iter([[]])
            mock_is_same_version.return_value = is_same_package_version

            download_info = list(ota_client_package.download_client_package(condition))
            assert len(download_info) == expected_download_info_length
            # prepare_manifest should always be called
            mock_prepare_manifest.assert_called_once()
            if is_same_package_version is False:
                # prepare_client_package should be called if version is different
                mock_prepare_client_package.assert_called_once()

    @pytest.mark.parametrize(
        "current_version, manifest_data, expected_result",
        [
            # 0: version match
            (
                "current_version",
                {
                    "schema_version": "1",
                    "date": "2025-03-13T00:00:00",
                    "packages": [
                        {
                            "filename": "package_x86_64.squashfs",
                            "version": "current_version",
                            "type": "squashfs",
                            "architecture": "x86_64",
                            "size": 123456,
                            "checksum": "abc123",
                        }
                    ],
                },
                True,
            ),
            # 1: version mismatch
            (
                "current_version",
                {
                    "schema_version": "1",
                    "date": "2025-03-13T00:00:00",
                    "packages": [
                        {
                            "filename": "package_x86_64.squashfs",
                            "version": "dummy_version",
                            "type": "squashfs",
                            "architecture": "x86_64",
                            "size": 123456,
                            "checksum": "abc123",
                        }
                    ],
                },
                False,
            ),
        ],
    )
    def test_is_same_client_package_version(
        self, ota_client_package, current_version, manifest_data, expected_result
    ):
        with patch("otaclient.client_package.__version__", new=current_version):
            ota_client_package._manifest = Manifest(**manifest_data)
            assert (
                ota_client_package.is_same_client_package_version() == expected_result
            )


class TestClientPackagePrepareter:
    """Test class for OTAClientPackagePreparer"""

    DUMMY_SQUASHFS_FILE = "/dummy/package.squashfs"
    DUMMY_MOUNT_BASE = "/dummy/test_mount_point"
    DUMMY_ACTIVE_ROOT = "/dummy/active_root"
    DUMMY_ACTIVE_SLOT_MNT_POINT = "/dummy/active_slot_mount_point"
    DUMMY_HOST_ROOT_MNT_POINT = "/dummy/host_root_mount_point"

    @pytest.fixture
    def package_prepareter(self):
        from otaclient.client_package import OTAClientPackagePreparer

        return OTAClientPackagePreparer(
            squashfs_file=self.DUMMY_SQUASHFS_FILE,
            mount_base=self.DUMMY_MOUNT_BASE,
            active_root=self.DUMMY_ACTIVE_ROOT,
            active_slot_mnt_point=self.DUMMY_ACTIVE_SLOT_MNT_POINT,
            host_root_mnt_point=self.DUMMY_HOST_ROOT_MNT_POINT,
            bootloader=MagicMock(),
        )

    @patch("otaclient.client_package.cmdhelper.ensure_mointpoint")
    @patch("otaclient.client_package.cmdhelper.ensure_umount")
    @patch("shutil.rmtree")
    def test_cleanup_mount_point(
        self,
        mock_rmtree,
        mock_ensure_umount,
        mock_ensure_mointpoint,
        package_prepareter,
    ):
        package_prepareter._cleanup_mount_point()

        mock_ensure_mointpoint.assert_called_once()
        mock_ensure_umount.assert_called_once()
        mock_rmtree.assert_called_once()

    @patch("otaclient.client_package.subprocess_call")
    @patch("otaclient.client_package.OTAClientPackagePreparer._unshare_wrapper")
    def test_create_mount_namespaces(
        self, mock_unshare_wrapper, mock_subprocess_call, package_prepareter
    ):
        package_prepareter._create_mount_namespaces()

        mock_unshare_wrapper.assert_called_once()
        mock_subprocess_call.assert_called_once_with(
            ["mount", "--make-rprivate", "/"], raise_exception=True
        )

    @patch("ctypes.CDLL", create=True)
    def test_unshare_wrapper_python_3_11(self, mock_cdll, package_prepareter):
        if sys.version_info >= (3, 12):
            pytest.skip("This test is for Python 3.11 and below only.")

        # Setup the mock correctly
        mock_libc = MagicMock()
        mock_cdll.return_value = mock_libc
        mock_libc.unshare.return_value = 0  # Success case

        # Call the function
        package_prepareter._unshare_wrapper()

        # Verify the mocks were called correctly
        mock_cdll.assert_called_once_with("libc.so.6", use_errno=True)
        mock_libc.unshare.assert_called_once()

    @patch("os.unshare", create=True)
    def test_unshare_wrapper_python_3_12(self, mock_unshare, package_prepareter):
        if sys.version_info < (3, 12):
            pytest.skip("This test is for Python 3.12 and above only.")

        # Call the function
        package_prepareter._unshare_wrapper()

        # Verify the mock was called correctly
        mock_unshare.assert_called_once()
