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

import shutil
from pathlib import Path
from typing import Generator

from ota_image_libs._resource_filter import CompressFilter
from ota_image_libs.v1.artifact.reader import OTAImageArtifactReader
from ota_image_libs.v1.image_config.schema import ImageConfig
from ota_image_libs.v1.image_manifest.schema import ImageManifest
from ota_image_libs.v1.otaclient_package.schema import OTAClientPackageManifest
from ota_image_libs.v1.resource_table.schema import ResourceTableManifestTypedDict
from ota_image_libs.v1.resource_table.utils import ResourceTableDBHelper

DB_FNAME = "resource_table.sqlite3"


class OTAImageBroken(Exception): ...


class OTAImageHelper:
    def __init__(self, _image_zip: Path, *, workdir: Path) -> None:
        self._workdir = workdir

        self._image_helper = OTAImageArtifactReader(_image_zip)
        self._resource_prefix = f"{self._image_helper._resource_dir.rstrip('/')}/"

        self._image_index = self._image_helper.parse_index()
        self._exclude_blobs = self._collect_protected_resources_digest()

    def _collect_protected_resources_digest(self) -> set[bytes]:
        """Scan through OTA image, collect blob digests that don't belong to any system image.

        These blobs will not be tracked by resource_table.
        """
        _res: set[bytes] = set()

        for manifest_descriptor in self._image_index.manifests:
            _res.add(manifest_descriptor.digest.digest)
            if isinstance(manifest_descriptor, ImageManifest.Descriptor):
                _manifest = ImageManifest.model_validate_json(
                    self._image_helper.read_blob_as_text(
                        manifest_descriptor.digest.digest_hex
                    )
                )

                for _file_table_descriptor in _manifest.layers:
                    _res.add(_file_table_descriptor.digest.digest)

                _image_config_descriptor = _manifest.config
                _res.add(_image_config_descriptor.digest.digest)

                _image_config = ImageConfig.model_validate_json(
                    self._image_helper.read_blob_as_text(
                        _image_config_descriptor.digest.digest_hex
                    )
                )
                if _sys_config_descriptor := _image_config.sys_config:
                    _res.add(_sys_config_descriptor.digest.digest)
                _res.add(_image_config.file_table.digest.digest)
            elif isinstance(manifest_descriptor, OTAClientPackageManifest.Descriptor):
                _manifest = OTAClientPackageManifest.model_validate_json(
                    self._image_helper.read_blob_as_text(
                        manifest_descriptor.digest.digest_hex
                    )
                )
                _res.add(_manifest.config.digest.digest)
                for _payload_descriptor in _manifest.layers:
                    _res.add(_payload_descriptor.digest.digest)
        return _res

    def save_index_json(self, _save_dst: Path) -> None:
        _save_dst.write_text(self._image_index.model_dump_json())

    def save_resource_table(self, _save_dst: Path) -> None:
        self._image_helper.get_resource_table(self._image_index, _save_dst)

    def iter_blob(self) -> Generator[bytes]:
        _zip = self._image_helper._f
        for _entry in _zip.namelist():
            if (
                _entry.startswith(self._resource_prefix)
                and _entry != self._resource_prefix
            ):
                _digest_hex = _entry.replace(self._resource_prefix, "")
                _digest = bytes.fromhex(_digest_hex)

                if _digest not in self._exclude_blobs:
                    yield _digest

    def save_blob(self, _digest: bytes, _save_dst: Path) -> int:
        with (
            self._image_helper.open_blob(_digest.hex()) as _src,
            open(_save_dst, "wb") as _dst,
        ):
            shutil.copyfileobj(_src, _dst)

        return _save_dst.stat().st_size


class ResourceTableHelper:
    def __init__(self, _rst_file: Path) -> None:
        _rst_db_helper = ResourceTableDBHelper(_rst_file)
        self._rst_orm = _rst_db_helper.get_orm()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self._rst_orm.__exit__(exc_type, exc, tb)

    def check_blob_zstd_compressed(self, _digest: bytes) -> bool:
        """Thread-safe helper to check whether a blob is zstd compressed."""

        _entry = self._rst_orm.orm_select_entry(
            ResourceTableManifestTypedDict(digest=_digest)
        )
        if not _entry:
            raise OTAImageBroken(f"resource with {_digest.hex()=} not found!")
        return isinstance(_entry.filter_applied, CompressFilter)
