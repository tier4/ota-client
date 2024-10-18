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

import re
from functools import partial
from typing import Callable

from simple_sqlite3_orm import ORMBase
from simple_sqlite3_orm._table_spec import TableSpecType

from ota_metadata._file_table.orm import RegularFilesORM
from ota_metadata._file_table.tables import (
    DirectoryTable,
    RegularFileTable,
    SymlinkTable,
)
from otaclient_common.typing import StrOrPath

BATCH_SIZE = 128
DIGEST_ALG = b"sha256"

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


def _parse_regular_line(_input: str) -> RegularFileTable:
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

    # _new.compressed_alg = (
    #     _compress_alg if (_compress_alg := _ma.group("compressed_alg")) else ""
    # )

    return _new


def import_regulars_txt(
    reginf_orm: RegularFilesORM,
    resinf_orm,
    csv_txt: StrOrPath,
    *,
    parser_func=_parse_regular_line,
):
    with open(csv_txt, "r") as f:
        _reginf_batch: list[RegularFileTable] = []
        _resinf_batch = []

        for line in f:
            _reg_inf, _res_inf = parser_func(line)
            _reginf_batch.append(_reg_inf)
            _resinf_batch.append(_res_inf)

            if len(_reginf_batch) >= BATCH_SIZE:
                _inserted = reginf_orm.orm_insert_entries(
                    _reginf_batch, or_option="ignore"
                )

                if _inserted != len(_reginf_batch):
                    raise ValueError("insert to database failed")
                _reginf_batch = []

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
