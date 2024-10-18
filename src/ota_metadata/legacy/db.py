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
"""Implementation of parsing ota metadata files and convert it to database."""


from __future__ import annotations

import os.path as os_path
import re
from functools import partial
from typing import Callable, ClassVar, Optional
from urllib.parse import quote

from simple_sqlite3_orm import ConstrainRepr, ORMBase, TableSpec, TypeAffinityRepr
from simple_sqlite3_orm._table_spec import TableSpecType
from typing_extensions import Annotated

from ota_metadata._file_table.orm import RegularFilesORM
from ota_metadata._file_table.tables import (
    DirectoryTable,
    RegularFileTable,
    SymlinkTable,
)
from otaclient_common.common import urljoin_ensure_base
from otaclient_common.typing import StrOrPath

BATCH_SIZE = 128
DIGEST_ALG = b"sha256"

# legacy OTA image specific resource table


class ResourceTable(TableSpec):
    sha256digest: Annotated[
        str,
        TypeAffinityRepr(str),
        ConstrainRepr("PRIMARY KEY"),
    ]
    compression_alg: Annotated[Optional[str], TypeAffinityRepr(str)] = None

    # NOTE: for backward compatible with revision 1
    path: Annotated[
        Optional[str],
        TypeAffinityRepr(str),
    ] = None

    size: Annotated[int, TypeAffinityRepr(int), ConstrainRepr("NOT NULL")]

    SUPPORTED_COMPRESSION_ALG: ClassVar[set[str]] = {"zst", "zstd"}

    def get_download_url(self, url_base: str) -> str:
        data_url = urljoin_ensure_base(url_base, "data")
        data_zst_url = urljoin_ensure_base(url_base, "data.zst")

        # v2 OTA image, with compression enabled
        # example: http://example.com/base_url/data.zstd/a665a45920422f9d417e4867efdc4fb8a04a1f3fff1fa07e998e86f7f7a27ae3.<compression_alg>
        if (
            compression_alg := self.compression_alg
        ) and compression_alg in self.SUPPORTED_COMPRESSION_ALG:
            return urljoin_ensure_base(
                data_zst_url,
                # NOTE: hex alpha-digits and dot(.) are not special character
                #       so no need to use quote here.
                f"{self.sha256digest}.{compression_alg}",
            )

        # v1 OTA image, uncompressed and use full path as URL path
        # example: http://example.com/base_url/data/rootfs/full/path/file
        if not self.path:
            raise ValueError("for uncompressed file, file path is required")
        return urljoin_ensure_base(
            data_url,
            quote(str(os_path.relpath(self.path, "/"))),
        )


class ResourceTableORM(ORMBase[ResourceTable]): ...


# CSV format OTA image metadata files regex pattern


def de_escape(s: str) -> str:
    return s.replace(r"'\''", r"'")


dir_pa = re.compile(r"(?P<mode>\d+),(?P<uid>\d+),(?P<gid>\d+),(?P<path>.*)")
symlink_pa = re.compile(
    r"(?P<mode>\d+),(?P<uid>\d+),(?P<gid>\d+),'(?P<link>.+)((?<!\')',')(?P<target>.+)'"
)
# NOTE(20221013): support previous regular_inf cvs version
#                 that doesn't contain size, inode and compressed_alg fields.
reginf_pa = re.compile(
    r"(?P<mode>\d+),(?P<uid>\d+),(?P<gid>\d+)"
    r",(?P<nlink>\d+),(?P<hash>\w+),'(?P<path>.+)'"
    r"(,(?P<size>\d+)?(,(?P<inode>\d+)?(,(?P<compressed_alg>\w+)?)?)?)?"
)


def _import_from_metadatafiles(
    orm: ORMBase[TableSpecType],
    csv_txt: StrOrPath,
    *,
    parser_func: Callable[[str], TableSpecType],
):
    with open(csv_txt, "r") as f:
        _batch: list[TableSpecType] = []

        for line in f:
            _batch.append(parser_func(line))

            if len(_batch) >= BATCH_SIZE:
                _inserted = orm.orm_insert_entries(_batch, or_option="ignore")

                if _inserted != len(_batch):
                    raise ValueError(f"{csv_txt}: insert to database failed")
                _batch = []

        if _batch:
            orm.orm_insert_entries(_batch, or_option="ignore")


def _parse_dir_line(_input: str) -> DirectoryTable:
    _ma = dir_pa.match(_input.strip())
    assert _ma is not None, f"matching dirs failed for {_input}"

    return DirectoryTable(
        mode=int(_ma.group("mode"), 8),
        uid=int(_ma.group("uid")),
        gid=int(_ma.group("gid")),
        path=de_escape(_ma.group("path")[1:-1]),
    )


import_dirs_txt = partial(_import_from_metadatafiles, parser_func=_parse_dir_line)


def _parse_symlink_line(_input: str) -> SymlinkTable:
    _ma = symlink_pa.match(_input.strip())
    assert _ma is not None, f"matching symlinks failed for {_input}"

    return SymlinkTable(
        path=de_escape(_ma.group("link")),
        uid=int(_ma.group("uid")),
        gid=int(_ma.group("gid")),
        target=de_escape(_ma.group("target")),
    )


import_symlinks_txt = partial(
    _import_from_metadatafiles, parser_func=_parse_symlink_line
)


def _parse_regular_line(_input: str) -> tuple[RegularFileTable, ResourceTable]:
    _ma = reginf_pa.match(_input.strip())
    assert _ma is not None, f"matching reg_inf failed for {_input}"

    _new = RegularFileTable(
        mode=int(_ma.group("mode"), 8),
        uid=int(_ma.group("uid")),
        gid=int(_ma.group("gid")),
        nlink=int(_ma.group("nlink")),
        digest=b":".join([DIGEST_ALG, bytes.fromhex(_ma.group("hash"))]),
        path=de_escape(_ma.group("path")),
        size=int(_ma.group("size")),
    )

    if (_inode := _ma.group("inode")) and (_inode := int(_inode)) > 1:
        _new.inode = _inode

    if _compress_alg := _ma.group("compressed_alg"):
        _new_resource = ResourceTable(
            sha256digest=_ma.group("hash"),
            size=_new.size,
            compression_alg=_compress_alg,
        )
    else:
        _new_resource = ResourceTable(
            sha256digest=_ma.group("hash"),
            size=_new.size,
            path=_new.path,
        )

    return _new, _new_resource


def import_regulars_txt(
    reginf_orm: RegularFilesORM,
    resinf_orm: ResourceTableORM,
    csv_txt: StrOrPath,
    *,
    parser_func: Callable[
        [str], tuple[RegularFileTable, ResourceTable]
    ] = _parse_regular_line,
):
    with open(csv_txt, "r") as f:
        _reginf_batch: list[RegularFileTable] = []
        _resinf_batch: list[ResourceTable] = []

        for line in f:
            _reg_inf, _res_inf = parser_func(line)
            _reginf_batch.append(_reg_inf)
            _resinf_batch.append(_res_inf)

            # NOTE: one file entry matches one resouce
            if len(_reginf_batch) >= BATCH_SIZE:
                _inserted = reginf_orm.orm_insert_entries(
                    _reginf_batch, or_option="ignore"
                )

                if _inserted != len(_reginf_batch):
                    raise ValueError("insert to database failed")
                _reginf_batch = []

                # NOTE: for duplicated resource insert, just ignore
                _inserted = resinf_orm.orm_insert_entries(
                    _resinf_batch, or_option="ignore"
                )
                if _inserted != len(_resinf_batch):
                    raise ValueError("insert to database failed")
                _resinf_batch = []

        if _reginf_batch:
            reginf_orm.orm_insert_entries(_reginf_batch, or_option="ignore")
        if _resinf_batch:
            resinf_orm.orm_insert_entries(_resinf_batch, or_option="ignore")
