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
"""OTA Image Spec Version1 support."""

from __future__ import annotations

import logging
import os
import threading
from copy import copy
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Generator, Literal

from ota_image_libs._crypto.x509_utils import CACertStore
from ota_image_libs.common import OCIDescriptor
from ota_image_libs.v1.consts import (
    IMAGE_INDEX_FNAME,
    INDEX_JWT_FNAME,
    RESOURCE_DIR,
    SUPPORTED_HASH_ALG,
)
from ota_image_libs.v1.file_table import FILE_TABLE_FNAME
from ota_image_libs.v1.file_table.db import FileTableDBHelper
from ota_image_libs.v1.image_config.schema import ImageConfig
from ota_image_libs.v1.image_config.sys_config import SysConfig
from ota_image_libs.v1.image_index.schema import ImageIdentifier, ImageIndex
from ota_image_libs.v1.image_manifest.schema import ImageManifest
from ota_image_libs.v1.index_jwt.utils import (
    decode_index_jwt_with_verification,
    get_index_jwt_sign_cert_chain,
)
from ota_image_libs.v1.otaclient_package.schema import OTAClientPackageManifest
from ota_image_libs.v1.resource_table import RESOURCE_TABLE_FNAME
from ota_image_libs.v1.resource_table.db import ResourceTableDBHelper

from otaclient_common.common import urljoin_ensure_base
from otaclient_common.download_info import DownloadInfo

from .errors import ImageMetadataInvalid, SignCertInvalid

logger = logging.getLogger(__name__)

IMAGE_MANIFEST_SAVE_FNAME = "image_manifest.json"
IMAGE_CONFIG_SAVE_FNAME = "image_config.json"
SYS_CONFIG_SAVE_FNAME = "sys_config.json"


def _get_arch() -> Literal["x86_64", "arm64"] | None:
    _arch = os.uname().machine
    if _arch in ["x86_64", "amd64"]:
        return "x86_64"
    if _arch in ["aarch64", "arm64"]:
        return "arm64"


class OTAImageHelper:
    def __init__(
        self, *, session_dir: Path, base_url: str, ca_store: CACertStore
    ) -> None:
        self._session_dir = session_dir
        self.base_url = base_url
        self.blob_storage_url = urljoin_ensure_base(base_url, RESOURCE_DIR)
        self._ca_store = ca_store

        # NOTE: to be downloaded
        self._index_jwt_fpath = session_dir / INDEX_JWT_FNAME
        self._image_index_fpath = session_dir / IMAGE_INDEX_FNAME
        self._image_manifest_fpath = session_dir / IMAGE_MANIFEST_SAVE_FNAME
        self._image_config_fpath = session_dir / IMAGE_CONFIG_SAVE_FNAME
        self._sys_config_fpath = session_dir / SYS_CONFIG_SAVE_FNAME
        self._file_table_dbf = session_dir / FILE_TABLE_FNAME
        self._resource_table_dbf = session_dir / RESOURCE_TABLE_FNAME

        # NOTE: to be parsed after downloading
        self.image_index: ImageIndex | None = None
        # NOTE: for each OTA, we will only select one image manifest
        self.image_manifest: ImageManifest | None = None
        self.image_config: ImageConfig | None = None
        self.sys_config: SysConfig | None = None

    @property
    def file_table_helper(self) -> FileTableDBHelper:
        return FileTableDBHelper(self._file_table_dbf)

    @property
    def resource_table_helper(self) -> ResourceTableDBHelper:
        return ResourceTableDBHelper(self._resource_table_dbf)

    def get_persistents_list(self) -> list[str] | None:
        if self.sys_config and self.sys_config.persist_files:
            return copy(self.sys_config.persist_files)

    def download_and_verify_image_index(
        self, condition: threading.Condition
    ) -> Generator[list[DownloadInfo]]:
        _index_jwt_fpath = self._index_jwt_fpath
        with condition:
            yield [
                DownloadInfo(
                    url=urljoin_ensure_base(self.base_url, INDEX_JWT_FNAME),
                    dst=_index_jwt_fpath,
                )
            ]
            condition.wait()  # wait for upper download the file

        logger.info("verify index.jwt signing cert ...")
        try:
            _index_jwt = _index_jwt_fpath.read_text()
            _sign_cert_chain = get_index_jwt_sign_cert_chain(_index_jwt)
        except Exception as e:
            raise ImageMetadataInvalid(f"index.jwt header is invalid: {e}") from e

        try:
            self._ca_store.verify(
                _sign_cert_chain.ee, interm_cas=_sign_cert_chain.interms
            )
        except Exception as e:
            raise SignCertInvalid(f"fail to verify sign cert: {e}") from e

        logger.info("verify index.json against index.jwt ...")
        try:
            _verified_claims = decode_index_jwt_with_verification(
                _index_jwt, _sign_cert_chain
            )
            _index_json_descriptor = _verified_claims.image_index
        except Exception as e:
            raise ImageMetadataInvalid(f"claims in index.jwt is invalid: {e}") from e

        _index_json_fpath = self._image_index_fpath
        with condition:
            yield [
                DownloadInfo(
                    url=urljoin_ensure_base(self.base_url, IMAGE_INDEX_FNAME),
                    dst=_index_json_fpath,
                    original_size=_index_json_descriptor.size,
                    digest=_index_json_descriptor.digest.digest_hex,
                    digest_alg=SUPPORTED_HASH_ALG,
                )
            ]
            condition.wait()

        logger.info("parse verified index.json ...")
        try:
            self.image_index = ImageIndex.parse_metafile(_index_json_fpath.read_text())
        except Exception as e:
            raise ImageMetadataInvalid(f"image index is invalid: {e}") from e

    def select_image_payload(
        self, _image_identifier: ImageIdentifier, condition: threading.Condition
    ) -> Generator[list[DownloadInfo]]:
        """Select one OTA image payload and download all the meta files required."""
        assert (_image_index := self.image_index)
        _manifest_descriptor = _image_index.find_image(_image_identifier)
        if not _manifest_descriptor:
            raise ImageMetadataInvalid(
                f"image indicated by {_image_identifier} cannot be found in OTA image!"
            )
        with condition:
            yield [
                self.download_from_descriptor(
                    self._image_manifest_fpath, _manifest_descriptor
                )
            ]
            condition.wait()

        try:
            self.image_manifest = _image_manifest = ImageManifest.parse_metafile(
                self._image_manifest_fpath.read_text()
            )
        except Exception as e:
            raise ImageMetadataInvalid(
                f"image manifest for {_image_identifier} is invalid: {e}"
            ) from e

        _image_config_descriptor = _image_manifest.config
        with condition:
            yield [
                self.download_from_descriptor(
                    self._image_config_fpath, _image_config_descriptor
                )
            ]
            condition.wait()

        try:
            self.image_config = _image_config = ImageConfig.parse_metafile(
                self._image_config_fpath.read_text()
            )
        except Exception as e:
            raise ImageMetadataInvalid(
                f"image config for {_image_identifier} is invalid: {e}"
            ) from e

        _sys_config_descriptor = _image_config.sys_config
        if _sys_config_descriptor:
            with condition:
                yield [
                    self.download_from_descriptor(
                        self._sys_config_fpath,
                        _sys_config_descriptor,
                    )
                ]
                condition.wait()

            try:
                self.sys_config = SysConfig.parse_metafile(
                    self._sys_config_fpath.read_text()
                )
            except Exception as e:
                raise ImageMetadataInvalid(
                    f"sys config for {_image_identifier} is invalid: {e}"
                ) from e

        # download the file_table and resource_table, note that file_table and resource_table
        #   are zstd compressed, we will also handle that here
        _file_table_descriptor = _image_config.file_table
        _resource_table_descriptor = self.image_index.image_resource_table
        assert _resource_table_descriptor
        with TemporaryDirectory(dir=self._session_dir) as _tmp_dir:
            with condition:
                _tmp_dir = Path(_tmp_dir)
                _ft_save = _tmp_dir / _file_table_descriptor.digest.digest_hex
                _rst_save = _tmp_dir / _resource_table_descriptor.digest.digest_hex
                yield [
                    self.download_from_descriptor(_ft_save, _file_table_descriptor),
                    self.download_from_descriptor(
                        _rst_save, _resource_table_descriptor
                    ),
                ]
                condition.wait()

            try:
                _file_table_descriptor.export_blob_from_resource_dir(
                    _tmp_dir, self._file_table_dbf, auto_decompress=True
                )
                _resource_table_descriptor.export_blob_from_resource_dir(
                    _tmp_dir, self._resource_table_dbf, auto_decompress=True
                )
            except Exception as e:
                raise ImageMetadataInvalid(
                    f"file_table and/or resource_table are invalid: {e}"
                ) from e

    def select_otaclient_package(
        self,
        save_dst: Path,
        version: str,
        *,
        condition: threading.Condition,
    ):
        assert self.image_index
        if not (_arch := _get_arch()):
            logger.warning("this machine is not either x86_64 or arm64 machine, abort")
            return

        _otaclient_package_manifests = self.image_index.find_otaclient_package()
        if not _otaclient_package_manifests:
            logger.info("not otaclient release package manifest found in the OTA image")
            return

        # NOTE: normally we will only put one otaclient release into the OTA image
        if len(_otaclient_package_manifests) != 1:
            logger.warning(
                "multiple otaclient package manifest found in the OTA image"
                "will pick the first one"
            )
        _otaclient_package_manifest_descriptor = _otaclient_package_manifests[0]

        _otaclient_manifest_fpath = (
            self._session_dir / "otaclient_release_manifest.json"
        )
        with condition:
            yield self.download_from_descriptor(
                _otaclient_manifest_fpath,
                _otaclient_package_manifest_descriptor,
            )
            condition.wait()

        _artifact = OTAClientPackageManifest.parse_metafile(
            _otaclient_manifest_fpath.read_text()
        ).find_package(version=version, architecture=_arch)
        if not _artifact:
            logger.warning(
                f"failed to find otaclient({version=}) app image for {_arch=}"
            )
            return

        with condition:
            yield self.download_from_descriptor(
                save_dst,
                _artifact,
            )
            condition.wait()

    def get_resource_url(self, digest_hex: str) -> str:
        return urljoin_ensure_base(self.blob_storage_url, digest_hex)

    def download_from_descriptor(
        self, save_dst: Path, oci_descriptor: OCIDescriptor
    ) -> DownloadInfo:
        """Return the <download_info> to download a resource pointed by <oci_descriptor>.

        NOTE: do not do decompression for downloading resource from descriptor!
              use corresponding API from OCIDescriptor to extract the file!
        """
        digest_hex = oci_descriptor.digest.digest_hex
        return DownloadInfo(
            url=urljoin_ensure_base(self.blob_storage_url, digest_hex),
            dst=save_dst,
            original_size=oci_descriptor.size,
            digest=digest_hex,
            digest_alg=SUPPORTED_HASH_ALG,
        )
