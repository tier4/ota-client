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

from functools import partial
from typing import Callable

from simple_sqlite3_orm import ORMBase
from simple_sqlite3_orm._table_spec import TableSpecType

from ota_metadata._file_table.orm import RegularFilesORM
from ota_metadata._file_table.tables import RegularFileTable
from ota_metadata.legacy.metafile_parser import (
    parse_dir_line,
    parse_regular_line,
    parse_symlink_line,
)
from ota_metadata.legacy.orm import ResourceTable, ResourceTableORM
from otaclient_common.typing import StrOrPath

BATCH_SIZE = 128
DIGEST_ALG = b"sha256"


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


import_dirs_txt = partial(_import_from_metadatafiles, parser_func=parse_dir_line)
import_symlinks_txt = partial(
    _import_from_metadatafiles, parser_func=parse_symlink_line
)


def import_regulars_txt(
    reginf_orm: RegularFilesORM,
    resinf_orm: ResourceTableORM,
    csv_txt: StrOrPath,
    *,
    parser_func: Callable[
        [str], tuple[RegularFileTable, ResourceTable]
    ] = parse_regular_line,
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
