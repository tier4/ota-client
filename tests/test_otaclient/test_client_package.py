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

from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest

from otaclient.client_package import (
    Manifest,
    OTAClientPackage,
    ReleasePackage,
)


@pytest.fixture
def ota_client_package():
    return OTAClientPackage(base_url="http://example.com", session_dir="/tmp/session")


def test_prepare_manifest(ota_client_package):
    condition = MagicMock()
    download_info = list(ota_client_package._prepare_manifest(condition))
    assert len(download_info) == 1
    assert download_info[0][0].url == "http://example.com/manifest.json"


@patch("builtins.open", new_callable=mock_open, read_data='{"packages": []}')
def test_prepare_manifest_load(mock_open, ota_client_package):
    condition = MagicMock()
    list(ota_client_package._prepare_manifest(condition))
    assert ota_client_package._manifest is not None
    assert isinstance(ota_client_package._manifest, Manifest)


def test_prepare_client_package(ota_client_package):
    condition = MagicMock()
    ota_client_package._manifest = Manifest(
        packages=[
            ReleasePackage(
                filename="package.squashfs", architecture="x86_64", type="squashfs"
            )
        ]
    )
    download_info = list(ota_client_package._prepare_client_package(condition))
    assert len(download_info) == 1
    assert download_info[0][0].url == "http://example.com/package.squashfs"


def test_get_available_package_metadata(ota_client_package):
    ota_client_package._manifest = Manifest(
        packages=[
            ReleasePackage(
                filename="package.squashfs", architecture="x86_64", type="squashfs"
            )
        ]
    )
    package = ota_client_package._get_available_package_metadata()
    assert package.filename == "package.squashfs"


@patch("subprocess.run")
def test_run_service(mock_subprocess, ota_client_package):
    ota_client_package.package = ReleasePackage(
        filename="package.squashfs", architecture="x86_64", type="squashfs"
    )
    ota_client_package.downloaded_package_file = Path("/tmp/package.squashfs")
    ota_client_package.current_squashfs_path = Path("/tmp/current.squashfs")
    ota_client_package.run_service()
    assert mock_subprocess.called


@patch("subprocess.run")
def test_finalize(mock_subprocess, ota_client_package):
    ota_client_package.finalize()
    assert mock_subprocess.called
