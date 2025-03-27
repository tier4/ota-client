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
"""OTA client package implementation."""


from __future__ import annotations

import json
import logging
import os.path
import platform
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Generator, Optional

from ota_metadata.legacy2.metadata import OTAMetadata
from otaclient import __version__
from otaclient.configs.cfg import cfg
from otaclient_common._typing import StrOrPath
from otaclient_common.common import urljoin_ensure_base
from otaclient_common.download_info import DownloadInfo
from otaclient_manifest.schema import Manifest, ReleasePackage

logger = logging.getLogger(__name__)


@dataclass
class OTAClientPackageDownloadInfo:
    url: str
    dst: Path
    filename: Optional[str] = None
    type: Optional[str] = None
    checksum: Optional[str] = None


class OTAClientPackage:
    """
    OTA session_dir layout:
    session_<session_id> /
        - / .download_<random> # the download area for OTA client package files
            - / .manifest.json # the manifest file
            - / .otaclient-${PLATFORM_SUFFIX}_v${BASE_VERSION}.squashfs # the OTA client package
            - / .otaclient-${PLATFORM_SUFFIX}_v${BASE_VERSION}-v${VERSION}.patch # the OTA client package patch file

    """

    ENTRY_POINT = cfg.OTACLIENT_INSTALLATION_RELEASE + "/manifest.json"
    ARCHITECTURE_X86_64 = "x86_64"
    ARCHITECTURE_ARM64 = "arm64"
    PACKAGE_TYPE_SQUASHFS = "squashfs"
    PACKAGE_TYPE_PATCH = "patch"

    def __init__(
        self,
        *,
        base_url: str,
        ota_metadata: OTAMetadata,
        session_dir: StrOrPath,
    ) -> None:
        self._base_url = base_url
        self._ota_metadata = ota_metadata
        self._session_dir = Path(session_dir)
        self._download_dir = self._session_dir / f".download_{os.urandom(4).hex()}"
        self._download_dir.mkdir(exist_ok=True, parents=True)

        self._manifest = None
        self.package = None

    @property
    def _rootfs_url(self) -> str:
        _metadata_jwt = self._ota_metadata.metadata_jwt
        if _metadata_jwt is None or _metadata_jwt.rootfs_directory is None:
            raise ValueError("metadata_jwt is not loaded yet, abort")
        return urljoin_ensure_base(
            self._base_url,
            f"{_metadata_jwt.rootfs_directory.strip('/')}/",
        )

    def _prepare_manifest(
        self,
        condition: threading.Condition,
    ) -> Generator[list[DownloadInfo]]:
        """Download raw manifest.json and parse it."""
        # ------ step 1: download manifest.json ------ #
        _client_manifest_fpath = self._download_dir / Path(self.ENTRY_POINT).name
        with condition:
            yield [
                DownloadInfo(
                    url=urljoin_ensure_base(
                        self._rootfs_url, self.ENTRY_POINT.lstrip("/")
                    ),
                    dst=_client_manifest_fpath,
                )
            ]
            condition.wait()  # wait for download finished

        # ------ step 2: load manifest.json ------ #
        with open(_client_manifest_fpath, "r") as f:
            manifest_data = json.load(f)
            self._manifest = Manifest(**manifest_data)

    def _prepare_client_package(
        self,
        condition: threading.Condition,
    ) -> Generator[list[DownloadInfo]]:
        """Download raw manifest.json and parse it."""

        # ------ step 1: decide the target package ------ #
        _available_package_metadata = self._get_available_package_metadata()

        # ------ step 2: download the target package ------ #
        _package_filename = _available_package_metadata.filename
        _package_file = cfg.OTACLIENT_INSTALLATION_RELEASE + "/" + _package_filename
        _downloaded_package_path = self._download_dir / _package_filename
        with condition:
            yield [
                DownloadInfo(
                    url=urljoin_ensure_base(
                        self._rootfs_url, _package_file.lstrip("/")
                    ),
                    dst=_downloaded_package_path,
                )
            ]
            condition.wait()  # wait for download finished
        self.package = _available_package_metadata
        self.downloaded_package_path = _downloaded_package_path

    def _get_available_package_metadata(self) -> ReleasePackage:
        """Get the available package for the current platform."""
        if self._manifest is None:
            raise ValueError("manifest.json is not loaded yet, abort")

        # ------ step 1: get current otaclient version and architecture ------ #
        _current_version = __version__
        _machine, _arch = platform.machine(), platform.processor()
        if _machine == "x86_64" or _arch == "x86_64":
            _architecture = self.ARCHITECTURE_X86_64
        elif _machine == "aarch64" or _arch == "aarch64":
            _architecture = self.ARCHITECTURE_ARM64
        else:
            raise ValueError(
                f"unsupported platform({_machine=}, {_arch=}) detected, abort"
            )

        # ------ step 2: check if squahfs package exists ------ #
        self.current_squashfs_path = Path(
            cfg.OTACLIENT_INSTALLATION_RELEASE
            + f"/otaclient-{_architecture}_v{_current_version}.squashfs"
        )
        _is_squashfs_exists = self.current_squashfs_path.is_file()

        # ------ step 3: find the target package ------ #
        # the schema of manifest.json is defined in .github/actions/generate_manifest/schema.py
        # first, try to find the patch file corresponding to the current squashfs
        if _is_squashfs_exists:
            for package in self._manifest.packages:
                if (
                    package.architecture == _architecture
                    and package.type == self.PACKAGE_TYPE_PATCH
                ):
                    if (
                        package.metadata is not None
                        and package.metadata.patch_base_version is not None
                        and package.metadata.patch_base_version == _current_version
                    ):
                        return package

        # if no patch file found, find the full package
        for package in self._manifest.packages:
            if (
                package.architecture == _architecture
                and package.type == self.PACKAGE_TYPE_SQUASHFS
            ):
                return package

        raise ValueError(
            f"No suitable package found for architecture {_architecture} and version {_current_version}"
        )

    def get_target_squashfs_path(self) -> Path:
        """Get the target squashfs file path."""
        if self.package is None:
            raise ValueError("OTA client package is not downloaded yet, abort")

        if self.package.type == self.PACKAGE_TYPE_PATCH:
            # apply patch to the existing squashfs
            _architecture = self.package.architecture
            # Validate architecture string to prevent injection
            if _architecture not in (self.ARCHITECTURE_X86_64, self.ARCHITECTURE_ARM64):
                raise ValueError(f"Invalid architecture: {_architecture}")

            _patch_path = self.downloaded_package_path
            _current_squashfs_path = self.current_squashfs_path
            _target_version = self.package.version

            # Validate version string to prevent injection
            if (
                not isinstance(_target_version, str)
                or not _target_version.replace(".", "").isalnum()
            ):
                raise ValueError(f"Invalid version string: {_target_version}")

            _target_squashfs_path = Path(
                cfg.OTACLIENT_INSTALLATION_RELEASE
                + f"/otaclient-{_architecture}_v{_target_version}.squashfs"
            )

            # Apply patch using subprocess with list arguments (safer)
            # and validate all paths are actually Path objects
            if not (
                isinstance(_current_squashfs_path, Path)
                and isinstance(_patch_path, Path)
                and isinstance(_target_squashfs_path, Path)
            ):
                raise TypeError("All paths must be Path objects")

            try:
                subprocess.run(
                    [
                        "zstd",
                        "-d",
                        f"--patch-from={_current_squashfs_path}",
                        str(_patch_path),
                        "-o",
                        str(_target_squashfs_path),
                    ],
                    check=True,
                    capture_output=True,  # Capture output to prevent terminal injection
                )
            except subprocess.CalledProcessError as e:
                logger.warning(f"failed to apply patch: {e.stderr.decode()}")
                raise
            return _target_squashfs_path

        # directly use the downloaded squashfs
        return self.downloaded_package_path

    # APIs

    def download_client_package(
        self,
        condition: threading.Condition,
    ) -> Generator[list[DownloadInfo]]:
        """Guide the caller to download ota client package by yielding the DownloadInfo instances.
        1. download and parse manifest.json
        2. download the target OTA client package.
        """
        try:
            yield from self._prepare_manifest(condition)
            yield from self._prepare_client_package(condition)
        except Exception as e:
            logger.exception(
                f"failure during downloading and verifying OTA client package: {e!r}"
            )
            raise

    def mount_squashfs(self):
        """Mount the squashfs file."""
        squashfs_path = self.get_target_squashfs_path()

        # Create a temporary directory to mount the squashfs
        _mount_dir = cfg.MOUNT_DIR
        os.makedirs(_mount_dir, exist_ok=True)

        try:
            # Mount the squashfs file
            subprocess.run(
                ["mount", "-t", "squashfs", str(squashfs_path), _mount_dir],
                check=True,
            )
        except Exception as e:
            logger.exception(f"failed to mount squashfs: {e!r}")
            raise
