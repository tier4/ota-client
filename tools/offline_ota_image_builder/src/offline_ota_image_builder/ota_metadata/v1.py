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
from zipfile import ZipFile

from ota_image_libs._resource_filter import CompressFilter
from ota_image_libs.v1.artifact.reader import OTAImageArtifactReader
from ota_image_libs.v1.resource_table.schema import ResourceTableManifestTypedDict
from ota_image_libs.v1.resource_table.utils import ResourceTableDBHelper

DB_FNAME = "resource_table.sqlite3"


class OTAImageBroken(Exception): ...


class OTAImageHelper:
    def __init__(self, _image_zip: Path) -> None:
        self._image_helper = OTAImageArtifactReader(_image_zip)
        self._resource_prefix = f"{self._image_helper._resource_dir.rstrip('/')}/"

        self._image_index = self._image_helper.parse_index()
        self._blob_names = self._get_blob_names_set(
            self._image_helper._f, prefix=self._resource_prefix
        )

    @staticmethod
    def _get_blob_names_set(_zipf: ZipFile, *, prefix: str) -> set[str]:
        _set = set()
        for _entry in _zipf.namelist():
            if _entry.startswith(prefix) and _entry != prefix:
                _set.add(_entry.replace(prefix, ""))
        return _set

    def save_index_json(self, _save_dst: Path) -> None:
        _save_dst.write_text(self._image_index.model_dump_json())

    def save_resource_table(self, _save_dst: Path) -> None:
        self._image_helper.get_resource_table(self._image_index, _save_dst)

    def lookup_and_check_blob(self, _digest: bytes) -> bool:
        _digest_hex = _digest.hex()
        if _digest_hex in self._blob_names:
            # already checked, no check again
            self._blob_names.discard(_digest_hex)
            return True
        return False

    def save_blob(self, _digest_hex: str, _save_dst: Path) -> int:
        with (
            self._image_helper.open_blob(_digest_hex) as _src,
            open(_save_dst, "wb") as _dst,
        ):
            shutil.copyfileobj(_src, _dst)

        return _save_dst.stat().st_size


def iter_resource_table(_rst_file: Path) -> Generator[tuple[bytes, bytes | None]]:
    """Iter throught the resource_table by resource_id order with checking whether blob is compressed.

    Yields:
        A tuple of digest of an origin blob, and if compressed, the compressed version of the blob.
    """
    _rst_db_helper = ResourceTableDBHelper(_rst_file)
    with (
        _rst_db_helper.get_orm() as orm_iter,
        _rst_db_helper.get_orm() as orm_lookup,
    ):
        for _entry in orm_iter.orm_select_all_with_pagination(batch_size=1024):
            _filter_applied = _entry.filter_applied

            # if a blob is filtered with CompressFilter, it MUST not present
            #   in the blob storage, but only the compressed version there.
            if isinstance(_filter_applied, CompressFilter):
                _compressed_blob = orm_lookup.orm_select_entry(
                    ResourceTableManifestTypedDict(
                        resource_id=_filter_applied.resource_id
                    )
                )
                yield _entry.digest, _compressed_blob.digest
            # for the offline OTA image, the compressed blob is also named
            #   with its original digest!
            else:
                yield _entry.digest, None
