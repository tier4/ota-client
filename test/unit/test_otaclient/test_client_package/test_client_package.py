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
"""Tests for OTAClientPackageDownloader."""

from __future__ import annotations

import json
import shutil
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

TEST_DATA_DIR = Path(__file__).parents[3] / "data" / "client_package"

DUMMY_URL = "http://example.com"
DUMMY_SESSION_DIR = "/tmp/session"
DUMMY_PACKAGE_INSTALL_DIR = "/dummy/otaclient_release"
DUMMY_SQUASHFS_FILE = "/dummy/package.squashfs"
DUMMY_MANIFEST_URL = "http://example.com/opt/ota/otaclient_release/manifest.json"
DUMMY_VALID_SHA256 = "a" * 64


def _make_package_dict(
    filename: str,
    package_type: str,
    architecture: str,
    *,
    version: str = "1.0.0",
    size: int = 123456,
    checksum: str = f"sha256:{DUMMY_VALID_SHA256}",
    patch_base_version: Optional[str] = None,
) -> dict:
    package: dict = {
        "filename": filename,
        "version": version,
        "type": package_type,
        "architecture": architecture,
        "size": size,
        "checksum": checksum,
    }
    if patch_base_version:
        package["metadata"] = {"patch_base_version": patch_base_version}
    return package


def _make_manifest_dict(packages: list[dict]) -> dict:
    return {
        "schema_version": "1",
        "date": "2025-03-13T00:00:00",
        "packages": packages,
    }


DUMMY_MANIFEST_DICT = _make_manifest_dict(
    [
        _make_package_dict("package.squashfs", "squashfs", "x86_64"),
    ]
)
DUMMY_MANIFEST_JSON = json.dumps(DUMMY_MANIFEST_DICT)


@pytest.fixture
def ota_client_package(tmp_path: Path) -> OTAClientPackageDownloader:
    mock_metadata = MagicMock()
    mock_metadata_jwt = MagicMock()
    mock_metadata_jwt.rootfs_directory = "opt/ota/otaclient_release"
    type(mock_metadata).metadata_jwt = PropertyMock(return_value=mock_metadata_jwt)

    session_dir = tmp_path / "session"
    session_dir.mkdir()

    return OTAClientPackageDownloader(
        base_url=DUMMY_URL,
        ota_metadata=mock_metadata,
        session_dir=str(session_dir),
        package_install_dir=DUMMY_PACKAGE_INSTALL_DIR,
        squashfs_file=DUMMY_SQUASHFS_FILE,
    )


# ============================================================
# _prepare_manifest
# ============================================================


class TestPrepareManifest:
    @patch(
        "otaclient.client_package.urljoin_ensure_base",
        return_value=DUMMY_MANIFEST_URL,
    )
    @patch("pathlib.Path.read_text", return_value=DUMMY_MANIFEST_JSON)
    def test_yields_download_info_for_manifest(
        self,
        _mock_read_text,
        _mock_url_join,
        ota_client_package: OTAClientPackageDownloader,
    ):
        condition = MagicMock()
        download_info = list(ota_client_package._prepare_manifest(condition))
        assert len(download_info) == 1
        assert download_info[0][0].url == DUMMY_MANIFEST_URL

    @patch("pathlib.Path.read_text", return_value=DUMMY_MANIFEST_JSON)
    def test_loads_manifest_after_download(
        self,
        _mock_read_text,
        ota_client_package: OTAClientPackageDownloader,
    ):
        condition = MagicMock()
        list(ota_client_package._prepare_manifest(condition))
        assert ota_client_package._manifest is not None
        assert isinstance(ota_client_package._manifest, Manifest)


# ============================================================
# _prepare_client_package — including hash verification
# ============================================================


class TestPrepareClientPackage:
    @patch("otaclient.client_package.urljoin_ensure_base")
    def test_yields_download_info_with_url_and_dst(
        self,
        mock_urljoin,
        ota_client_package: OTAClientPackageDownloader,
    ):
        mock_urljoin.return_value = (
            "http://example.com/opt/ota/otaclient_release/package.squashfs"
        )
        condition = MagicMock()
        ota_client_package._manifest = Manifest(**DUMMY_MANIFEST_DICT)

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
        "checksum, expected_digest_alg, expected_digest",
        [
            pytest.param(
                f"sha256:{'a' * 64}",
                "sha256",
                "a" * 64,
                id="sha256_checksum",
            ),
            pytest.param(
                f"sha512:{'b' * 128}",
                "sha512",
                "b" * 128,
                id="sha512_checksum",
            ),
        ],
    )
    @patch("otaclient.client_package.urljoin_ensure_base", return_value="http://x/p")
    def test_download_info_includes_digest_fields(
        self,
        _mock_urljoin,
        ota_client_package: OTAClientPackageDownloader,
        checksum: str,
        expected_digest_alg: str,
        expected_digest: str,
    ):
        manifest_dict = _make_manifest_dict(
            [
                _make_package_dict(
                    "pkg.squashfs",
                    "squashfs",
                    "x86_64",
                    checksum=checksum,
                    size=999,
                ),
            ]
        )
        condition = MagicMock()
        ota_client_package._manifest = Manifest(**manifest_dict)

        download_info = list(ota_client_package._prepare_client_package(condition))

        di = download_info[0][0]
        assert di.digest_alg == expected_digest_alg
        assert di.digest == expected_digest
        assert di.original_size == 999

    @pytest.mark.parametrize(
        "bad_checksum",
        [
            pytest.param("", id="empty_string"),
            pytest.param("nocolon", id="no_colon_separator"),
            pytest.param(":abcdef", id="missing_algorithm"),
            pytest.param("sha256:", id="missing_digest"),
        ],
    )
    @patch("otaclient.client_package.urljoin_ensure_base", return_value="http://x/p")
    def test_raises_on_invalid_checksum_format(
        self,
        _mock_urljoin,
        ota_client_package: OTAClientPackageDownloader,
        bad_checksum: str,
    ):
        manifest_dict = _make_manifest_dict(
            [
                _make_package_dict(
                    "pkg.squashfs",
                    "squashfs",
                    "x86_64",
                    checksum=bad_checksum,
                ),
            ]
        )
        condition = MagicMock()
        ota_client_package._manifest = Manifest(**manifest_dict)

        with pytest.raises(ValueError, match="invalid checksum format"):
            list(ota_client_package._prepare_client_package(condition))


# ============================================================
# _get_available_package_metadata
# ============================================================


class TestGetAvailablePackageMetadata:
    @pytest.mark.parametrize(
        "machine, arch, is_squashfs_exists, is_zstd_supported, manifest_data, expected_filename",
        [
            pytest.param(
                "x86_64",
                "x86_64",
                False,
                True,
                _make_manifest_dict(
                    [
                        _make_package_dict("pkg_arm64.squashfs", "squashfs", "arm64"),
                        _make_package_dict("pkg_x86_64.squashfs", "squashfs", "x86_64"),
                    ]
                ),
                "pkg_x86_64.squashfs",
                id="x86_64_selects_matching_squashfs",
            ),
            pytest.param(
                "aarch64",
                "aarch64",
                False,
                True,
                _make_manifest_dict(
                    [
                        _make_package_dict("pkg_arm64.squashfs", "squashfs", "arm64"),
                        _make_package_dict("pkg_x86_64.squashfs", "squashfs", "x86_64"),
                    ]
                ),
                "pkg_arm64.squashfs",
                id="aarch64_selects_matching_squashfs",
            ),
            pytest.param(
                "x86_64",
                "x86_64",
                False,
                True,
                _make_manifest_dict(
                    [
                        _make_package_dict("pkg_x86_64.squashfs", "squashfs", "x86_64"),
                        _make_package_dict(
                            "patch_x86_64.patch",
                            "patch",
                            "x86_64",
                            patch_base_version=__version__,
                        ),
                    ]
                ),
                "pkg_x86_64.squashfs",
                id="no_squashfs_file_falls_back_to_full_package",
            ),
            pytest.param(
                "x86_64",
                "x86_64",
                True,
                True,
                _make_manifest_dict(
                    [
                        _make_package_dict("pkg_x86_64.squashfs", "squashfs", "x86_64"),
                        _make_package_dict(
                            "patch_x86_64.patch",
                            "patch",
                            "x86_64",
                            patch_base_version="dummy_version",
                        ),
                    ]
                ),
                "pkg_x86_64.squashfs",
                id="version_mismatch_falls_back_to_full_package",
            ),
            pytest.param(
                "x86_64",
                "x86_64",
                True,
                True,
                _make_manifest_dict(
                    [
                        _make_package_dict("pkg_x86_64.squashfs", "squashfs", "x86_64"),
                        _make_package_dict(
                            "patch_x86_64.patch",
                            "patch",
                            "x86_64",
                            patch_base_version=__version__,
                        ),
                    ]
                ),
                "patch_x86_64.patch",
                id="version_match_selects_patch",
            ),
            pytest.param(
                "x86_64",
                "x86_64",
                True,
                False,
                _make_manifest_dict(
                    [
                        _make_package_dict("pkg_x86_64.squashfs", "squashfs", "x86_64"),
                        _make_package_dict(
                            "patch_x86_64.patch",
                            "patch",
                            "x86_64",
                            patch_base_version=__version__,
                        ),
                    ]
                ),
                "pkg_x86_64.squashfs",
                id="no_zstd_falls_back_to_full_package",
            ),
        ],
    )
    def test_selects_correct_package(
        self,
        ota_client_package: OTAClientPackageDownloader,
        machine: str,
        arch: str,
        is_squashfs_exists: bool,
        is_zstd_supported: bool,
        manifest_data: dict,
        expected_filename: str,
    ):
        ota_client_package._manifest = Manifest(**manifest_data)

        with (
            patch("platform.machine", return_value=machine),
            patch("platform.processor", return_value=arch),
            patch.object(Path, "is_file", return_value=is_squashfs_exists),
            patch(
                "otaclient.client_package.shutil.which",
                return_value="/fake/zstd" if is_zstd_supported else None,
            ),
        ):
            package = ota_client_package._get_available_package_metadata()
            assert package.filename == expected_filename

    def test_raises_on_unsupported_architecture(
        self,
        ota_client_package: OTAClientPackageDownloader,
    ):
        ota_client_package._manifest = Manifest(**DUMMY_MANIFEST_DICT)

        with (
            patch("platform.machine", return_value="riscv64"),
            patch("platform.processor", return_value="riscv64"),
            pytest.raises(ValueError, match="unsupported platform"),
        ):
            ota_client_package._get_available_package_metadata()

    def test_raises_when_no_matching_package(
        self,
        ota_client_package: OTAClientPackageDownloader,
    ):
        manifest_dict = _make_manifest_dict(
            [_make_package_dict("pkg_arm64.squashfs", "squashfs", "arm64")]
        )
        ota_client_package._manifest = Manifest(**manifest_dict)

        with (
            patch("platform.machine", return_value="x86_64"),
            patch("platform.processor", return_value="x86_64"),
            pytest.raises(ValueError, match="No suitable package found"),
        ):
            ota_client_package._get_available_package_metadata()


# ============================================================
# _get_target_squashfs_path
# ============================================================


class TestGetTargetSquashfsPath:
    @pytest.mark.parametrize(
        "package_type, expect_downloaded_path",
        [
            pytest.param("squashfs", True, id="squashfs_returns_downloaded_path"),
            pytest.param("patch", False, id="patch_returns_session_path"),
        ],
    )
    def test_returns_correct_path_by_type(
        self,
        ota_client_package: OTAClientPackageDownloader,
        package_type: str,
        expect_downloaded_path: bool,
    ):
        ota_client_package.package = MagicMock()
        ota_client_package.package.type = package_type
        session_dir = Path("/tmp/session")
        ota_client_package._session_dir = session_dir
        ota_client_package.downloaded_package_path = (
            session_dir / ".download" / "package.squashfs"
        )
        ota_client_package._create_squashfs_from_patch = MagicMock()

        target = ota_client_package._get_target_squashfs_path()

        if expect_downloaded_path:
            assert target == session_dir / ".download" / "package.squashfs"
        else:
            assert target == session_dir / "package.squashfs"


# ============================================================
# _create_squashfs_from_patch
# ============================================================


class TestCreateSquashfsFromPatch:
    def test_applies_patch_to_squashfs(
        self,
        ota_client_package: OTAClientPackageDownloader,
        tmp_path: Path,
    ):
        if not shutil.which("zstd"):
            pytest.skip("zstd is not available")

        test_squashfs = tmp_path / "v1.squashfs"
        test_patch = tmp_path / "v1_v2.patch"
        shutil.copy(TEST_DATA_DIR / "v1.squashfs", test_squashfs)
        shutil.copy(TEST_DATA_DIR / "v1_v2.patch", test_patch)

        ota_client_package.package = MagicMock()
        ota_client_package.package.architecture = "x86_64"
        ota_client_package.package.type = "patch"
        ota_client_package.package.version = "1.2.3"
        ota_client_package._session_dir = tmp_path
        ota_client_package.downloaded_package_path = test_patch
        ota_client_package.current_squashfs_path = test_squashfs

        target = tmp_path / "output.squashfs"
        assert not target.exists()

        ota_client_package._create_squashfs_from_patch(target)

        assert target.exists()


# ============================================================
# download_client_package
# ============================================================


class TestDownloadClientPackage:
    @pytest.mark.parametrize(
        "is_same_version, expected_yields",
        [
            pytest.param(True, 1, id="same_version_skips_package_download"),
            pytest.param(False, 2, id="different_version_downloads_package"),
        ],
    )
    def test_orchestrates_manifest_and_package_download(
        self,
        ota_client_package: OTAClientPackageDownloader,
        is_same_version: bool,
        expected_yields: int,
    ):
        condition = threading.Condition()

        with (
            patch.object(
                ota_client_package, "_prepare_manifest", return_value=iter([[]])
            ) as mock_manifest,
            patch.object(
                ota_client_package,
                "_prepare_client_package",
                return_value=iter([[]]),
            ) as mock_package,
            patch.object(
                ota_client_package,
                "is_same_client_package_version",
                return_value=is_same_version,
            ),
        ):
            download_info = list(ota_client_package.download_client_package(condition))

            assert len(download_info) == expected_yields
            mock_manifest.assert_called_once()
            if is_same_version:
                mock_package.assert_not_called()
            else:
                mock_package.assert_called_once()


# ============================================================
# is_same_client_package_version
# ============================================================


class TestIsSameClientPackageVersion:
    @pytest.mark.parametrize(
        "current_version, package_version, expected",
        [
            pytest.param("1.0.0", "1.0.0", True, id="versions_match"),
            pytest.param("1.0.0", "2.0.0", False, id="versions_differ"),
        ],
    )
    def test_compares_versions(
        self,
        ota_client_package: OTAClientPackageDownloader,
        current_version: str,
        package_version: str,
        expected: bool,
    ):
        manifest_dict = _make_manifest_dict(
            [
                _make_package_dict(
                    "pkg.squashfs",
                    "squashfs",
                    "x86_64",
                    version=package_version,
                ),
            ]
        )
        with patch("otaclient.client_package.__version__", new=current_version):
            ota_client_package._manifest = Manifest(**manifest_dict)
            assert ota_client_package.is_same_client_package_version() == expected

    def test_returns_false_when_no_manifest(
        self,
        ota_client_package: OTAClientPackageDownloader,
    ):
        assert ota_client_package._manifest is None
        assert ota_client_package.is_same_client_package_version() is False
