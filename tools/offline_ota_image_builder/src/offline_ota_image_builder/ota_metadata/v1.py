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
from ota_image_libs.v1.resource_table.schema import ResourceTableManifestTypedDict
from ota_image_libs.v1.resource_table.utils import ResourceTableDBHelper

DB_CONN = 3
DB_FNAME = "resource_table.sqlite3"


class OTAImageBroken(Exception): ...


class OTAImageHelper:
    def __init__(self, _image_zip: Path) -> None:
        self._image_helper = OTAImageArtifactReader(_image_zip)
        self._resource_prefix = self._image_helper._resource_dir

    def save_resource_table(self, _save_dst: Path) -> None:
        _image_index = self._image_helper.parse_index()
        self._image_helper.get_resource_table(_image_index, _save_dst)

    def iter_blob(self) -> Generator[str]:
        _zip = self._image_helper._f
        for _entry in _zip.namelist():
            if (
                _entry.startswith(self._resource_prefix)
                and _entry != f"{self._resource_prefix}/"
            ):
                yield _entry.replace(self._resource_prefix, "")

    def save_blob(self, _digest_hex: str, _save_dst: Path) -> None:
        with (
            self._image_helper.open_blob(_digest_hex) as _src,
            open(_save_dst, "wb") as _dst,
        ):
            shutil.copyfileobj(_src, _dst)


class ResourceTableHelper:
    def __init__(self, _rst_file: Path, *, db_conn: int = DB_CONN) -> None:
        _rst_db_helper = ResourceTableDBHelper(_rst_file)
        self._rst_orm_pool = _rst_db_helper.get_orm_pool(db_conn)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self._rst_orm_pool.orm_pool_shutdown()

    def check_blob_zstd_compressed(self, _digest_hex: str) -> bool:
        """Thread-safe helper to check whether a blob is zstd compressed."""

        _entry = self._rst_orm_pool.orm_select_entry(
            ResourceTableManifestTypedDict(digest=bytes.fromhex(_digest_hex))
        )
        if not _entry:
            raise OTAImageBroken(f"resource with {_digest_hex=} not found!")
        return isinstance(_entry.filter_applied, CompressFilter)
