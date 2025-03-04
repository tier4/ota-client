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
"""OTA client package implementation.

OTA client package release format definition: https://tier4.atlassian.net/wiki/spaces/OTA/pages/3486056633/4.+The+release+format+of+otaclient

"""


from __future__ import annotations

import json
import logging
import os.path
import platform
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Generator, Optional

from otaclient import __version__
from otaclient.configs.cfg import cfg
from otaclient_common._typing import StrOrPath
from otaclient_common.common import urljoin_ensure_base
from otaclient_common.downloader import DownloadInfo

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

    def __init__(
        self,
        *,
        base_url: str,
        session_dir: StrOrPath,
    ) -> None:
        self._base_url = base_url
        self._session_dir = Path(session_dir)

        self._manifest = None

    def _prepare_manifest(
        self,
        _download_dir: Path,
        condition: threading.Condition,
    ) -> Generator[list[DownloadInfo]]:
        """Download raw manifest.json and parse it."""

        # ------ step 1: download manifest.json ------ #
        _client_manifest_fpath = _download_dir / Path(self.ENTRY_POINT).name
        with condition:
            yield [
                DownloadInfo(
                    url=urljoin_ensure_base(self._base_url, self.ENTRY_POINT),
                    dst=_client_manifest_fpath,
                )
            ]
            condition.wait()  # wait for download finished

        # ------ step 2: load manifest.json ------ #
        with open(_client_manifest_fpath, "r") as f:
            self._manifest = json.load(f)

    def _prepare_client_package(
        self,
        _download_dir: Path,
        condition: threading.Condition,
    ) -> Generator[list[DownloadInfo]]:
        """Download raw manifest.json and parse it."""

        # ------ step 1: decide the target package ------ #
        _available_package = self._get_available_package()

        # ------ step 2: download the target package ------ #
        _package_name = _available_package["filename"]
        _package_path = cfg.OTACLIENT_INSTALLATION_RELEASE / _package_name
        _client_package_fpath = _download_dir / _package_name
        with condition:
            yield [
                DownloadInfo(
                    url=urljoin_ensure_base(self._base_url, _package_path),
                    dst=_client_package_fpath,
                    type=_available_package.get("type", None),
                    checksum=_available_package.get("checksum", None),
                )
            ]
            condition.wait()

    def _get_available_package(self) -> dict:
        """Get the available package for the current platform."""
        if self._manifest is None:
            raise ValueError("manifest.json is not loaded yet, abort")

        # ------ step 1: get current otaclient version and architecture ------ #
        _version = {__version__}
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
        _squashfs_file = Path(
            cfg.OTACLIENT_INSTALLATION_RELEASE
            / f".otaclient-{_architecture}_v{__version__}.squashfs"
        )
        _is_squashfs_exists = _squashfs_file.is_file()

        # ------ step 3: find the target package ------ #
        # the schema of manifest.json is defined in .github/actions/generate_manifest/schema.py
        # first, try to find the patch file
        if _is_squashfs_exists:
            for package in self._manifest["packages"]:
                if (
                    package["architecture"] == _architecture
                    and package["type"] == "patch"
                ):
                    metadata = package.get("metadata", {})
                    if metadata.get("patch_base_version") == _version:
                        return package

        # if no patch file found, find the full package
        for package in self._manifest["packages"]:
            if (
                package["architecture"] == _architecture
                and package["type"] == "squashfs"
            ):
                return package

        raise ValueError(
            f"No suitable package found for architecture {_architecture} and version {_version}"
        )

    # APIs

    def download_client_package(
        self,
        condition: threading.Condition,
    ) -> Generator[list[DownloadInfo]]:
        """Guide the caller to download ota client package by yielding the DownloadInfo instances.
        1. download and parse manifest.json
        2. download the target OTA client package.
        """
        _download_dir = df = self._session_dir / f".download_{os.urandom(4).hex()}"
        df.mkdir(exist_ok=True, parents=True)

        try:
            yield from self._prepare_manifest(_download_dir, condition)
            yield from self._prepare_client_package(_download_dir, condition)
        except Exception as e:
            logger.exception(
                f"failure during downloading and verifying OTA client package: {e!r}"
            )
            raise
        finally:
            shutil.rmtree(_download_dir, ignore_errors=True)
