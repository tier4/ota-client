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
import os
import platform
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Generator, Optional

from ota_metadata.legacy2.metadata import OTAMetadata
from otaclient import __version__
from otaclient.configs.cfg import cfg
from otaclient_common import cmdhelper
from otaclient_common._typing import StrOrPath
from otaclient_common.common import (
    subprocess_call,
    urljoin_ensure_base,
)
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


class OTAClientPackageDownloader:
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
        self.current_squashfs_path = (
            Path(cfg.OTACLIENT_INSTALLATION_RELEASE)
            / Path(cfg.DYNAMIC_CLIENT_SQUASHFS_FILE).name
        )
        _is_squashfs_exists = self.current_squashfs_path.is_file()
        _is_zstd_supported = shutil.which("zstd") is not None

        # ------ step 3: find the target package ------ #
        # the schema of manifest.json is defined in otaclient_manifest/schema.py
        # first, try to find the patch file corresponding to the current squashfs
        if _is_squashfs_exists and _is_zstd_supported:
            for package in self._manifest.packages:
                if (
                    package.architecture == _architecture
                    and package.type == self.PACKAGE_TYPE_PATCH
                    and package.metadata
                    and package.metadata.patch_base_version
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

    def _get_target_squashfs_path(self) -> Path:
        """Get the target squashfs file path."""
        if self.package is None:
            raise ValueError("OTA client package is not downloaded yet, abort")

        if self.package.type == self.PACKAGE_TYPE_PATCH:
            _target_squashfs_path = (
                Path(self._session_dir) / Path(cfg.DYNAMIC_CLIENT_SQUASHFS_FILE).name
            )
            if not _target_squashfs_path.is_file():
                self._create_squashfs_from_patch(_target_squashfs_path)
            return _target_squashfs_path

        # directly use the downloaded squashfs
        return self.downloaded_package_path

    def _create_squashfs_from_patch(self, target_squashfs_path: Path) -> None:
        """Create a squashfs file from the patch file."""
        if self.package is None:
            raise ValueError("OTA client package is not downloaded yet, abort")
        if shutil.which("zstd") is None:
            raise ValueError("zstd is not installed, abort")

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

        # Apply patch using subprocess with list arguments (safer)
        # and validate all paths are actually Path objects
        if not (
            isinstance(_current_squashfs_path, Path)
            and isinstance(_patch_path, Path)
            and isinstance(target_squashfs_path, Path)
        ):
            raise TypeError("All paths must be Path objects")

        _cmd = [
            "zstd",
            "-d",
            f"--patch-from={_current_squashfs_path}",
            str(_patch_path),
            "-o",
            str(target_squashfs_path),
        ]
        logger.info(f"Applying patch with command: {' '.join(_cmd)}")
        try:
            subprocess_call(
                _cmd,
                raise_exception=True,
            )
        except subprocess.CalledProcessError as e:
            logger.warning(f"failed to apply patch: {e!r}")
            raise

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
            if not self.is_same_client_package_version():
                yield from self._prepare_client_package(condition)
            else:
                logger.info("Skipping client package download as version is unchanged")
        except Exception as e:
            logger.exception(
                f"failure during downloading and verifying OTA client package: {e!r}"
            )

    def is_same_client_package_version(self) -> bool:
        """Check if the current OTA client package version is the same as the target version in manifest"""
        if self._manifest is None:
            return False

        for package in self._manifest.packages:
            if package.version == __version__:
                return True
        return False

    def copy_client_package(self) -> None:
        """Copy the client package."""
        _squashfs_file = cfg.DYNAMIC_CLIENT_SQUASHFS_FILE
        # copy the squashfs file
        os.makedirs(os.path.dirname(_squashfs_file), exist_ok=True)
        shutil.copy(self._get_target_squashfs_path(), _squashfs_file)


class OTAClientPackagePrepareter:

    def _cleanup_mount_point(self, mount_base: StrOrPath) -> None:
        """Cleanup the mount point."""
        try:
            # unmount the dynamic client mount point
            cmdhelper.ensure_mointpoint(mount_base, ignore_error=True)
            cmdhelper.ensure_umount(
                mount_base, ignore_error=False, max_retry=0, retry_interval=0
            )
        except Exception as e:
            logger.warning(f"error while unmounting dynamic client mount point: {e}")

        try:
            # remove the dynamic client mount point
            shutil.rmtree(mount_base, ignore_errors=False)
        except Exception as e:
            logger.warning(f"error while removing dynamic client mount point: {e}")

    def _create_mount_namespaces(self) -> None:
        """Create mount namespaces for the current process."""
        # create a new mount namespace
        self._unshare_wrapper()
        # Make all mounts private to prevent propagation
        subprocess_call(["mount", "--make-rprivate", "/"], raise_exception=True)

    def _unshare_wrapper(self) -> None:
        """Unshare the mount namespace."""
        logger.info("migrating to a new mount namespace by unsharing")
        if sys.version_info >= (3, 12):
            # Python 3.12+ has native os.unshare support
            os.unshare(os.CLONE_NEWNS)
        else:
            # For older versions, use system call directly
            import ctypes

            CLONE_NEWNS = 0x00020000  # From linux/sched.h
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            if libc.unshare(CLONE_NEWNS) != 0:
                errno = ctypes.get_errno()
                raise OSError(
                    errno, f"Failed to unshare mount namespace: {os.strerror(errno)}"
                )

    def _mount_squashfs_file(
        self,
        squashfs_file: StrOrPath,
        mount_base: StrOrPath,
    ) -> None:
        """Mount the squashfs file to the mount base."""
        # check if the squashfs file exists
        if not os.path.exists(squashfs_file):
            raise ValueError(f"Squashfs file does not exist: {squashfs_file}")

        # mount the squashfs file
        cmdhelper.ensure_mointpoint(
            mount_base,
            ignore_error=False,
        )

        cmdhelper.ensure_mount(
            target=squashfs_file,
            mnt_point=mount_base,
            mount_func=cmdhelper.mount_squashfs,
            raise_exception=True,
        )

    def _bind_mount_host_dirs(self, mount_base: StrOrPath) -> None:
        """Bind mount the host directories to the mount base."""
        # check if the mount base exists
        if not os.path.exists(mount_base):
            raise ValueError(f"Mount base does not exist: {mount_base}")

        def bind_paths(paths: list[str], mount_base: StrOrPath, mount_func) -> None:
            for _path in paths:
                if not os.path.exists(_path):
                    logger.warning(f"bind path does not exist: {_path}, skipping")
                    continue

                _mount_point = f"{mount_base}{_path}"
                cmdhelper.ensure_mointpoint(
                    _mount_point,
                    ignore_error=False,
                )
                cmdhelper.ensure_mount(
                    target=_path,
                    mnt_point=_mount_point,
                    mount_func=mount_func,
                    raise_exception=True,
                )

        # bind necessary directories
        # should not include /bin, /lib, /usr, /sbin because they are included in the squashfs
        RW_PATHS = [
            "/boot",
            "/boot/firmware",
            "/dev",
            "/dev/shm",
            "/etc",
            "/home",
            "/mnt",
            "/opt",
            "/ota-cache",
            "/proc",
            "/root",
            "/run",
            "/sys",
            "/tmp",
            "/usr/share/ca-certificates",
            "/var",
        ]

        bind_paths(
            paths=RW_PATHS, mount_base=mount_base, mount_func=cmdhelper.bind_mount_rw
        )

    def _rbind_mount_active_slot(self, mount_base: StrOrPath) -> None:
        """Mount the active slot to the mount base."""
        # After chroot, the active slot root is not accessible from the chroot environment.
        # So we need to bind mount the active slot before chroot.

        # check if the mount base exists
        if not os.path.exists(mount_base):
            raise ValueError(f"Mount base does not exist: {mount_base}")

        _mount_point = f"{mount_base}{cfg.ACTIVE_SLOT_MNT}"
        logger.info(f"mounting {cfg.ACTIVE_ROOT} to {_mount_point}")
        cmdhelper.ensure_mointpoint(
            _mount_point,
            ignore_error=True,
        )

        cmdhelper.ensure_mount(
            target=cfg.ACTIVE_ROOT,
            mnt_point=_mount_point,
            mount_func=cmdhelper.rbind_mount_ro,
            raise_exception=True,
        )

    # APIs

    def mount_client_package(self) -> None:
        """Mount the client package to the mount base."""
        _squashfs_file = cfg.DYNAMIC_CLIENT_SQUASHFS_FILE

        _mount_base = cfg.DYNAMIC_CLIENT_MNT
        os.makedirs(_mount_base, exist_ok=True)
        try:
            logger.info(f"mounting {_squashfs_file} to {_mount_base}")
            self._cleanup_mount_point(_mount_base)
            self._create_mount_namespaces()
            self._mount_squashfs_file(_squashfs_file, _mount_base)
            self._bind_mount_host_dirs(_mount_base)
            self._rbind_mount_active_slot(_mount_base)

            logger.info("mounted squashfs successfully")
        except subprocess.CalledProcessError as e:
            logger.exception(f"failed to mount squashfs: {e!r}")
            raise
