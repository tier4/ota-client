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


from __future__ import annotations

from functools import partial
from typing import Generator

from simple_sqlite3_orm import ORMBase, ORMThreadPoolBase

from ._table import (
    FileTableDirectories,
    FileTableNonRegularFiles,
    FileTableRegularFiles,
)

FT_REGULAR_TABLE_NAME = "ft_regular"
FT_NON_REGULAR_TABLE_NAME = "ft_non_regular"
FT_DIR_TABLE_NAME = "ft_dir"


class FileTableRegularORM(ORMBase[FileTableRegularFiles]):

    _orm_table_name = FT_REGULAR_TABLE_NAME

    def iter_common_by_digest(self, other_db: str) -> Generator[FileTableRegularFiles]:
        """Yield entries in this ft which digest presented on <other_db>.ft_table.

        This is for assisting faster delta_calculation without full disk scan.
        """

        attached_db_schema = "base"
        stmt = """\
        SELECT d2.*
        FROM base.ft_regular AS d2
        INNER JOIN (
            SELECT digest
            FROM main.ft_regular
            GROUP BY digest
        ) AS d1 ON d2.digest = d1.digest;
        """
        orm_conn = self.orm_con

        orm_conn.execute(f"ATTACH {other_db} AS {attached_db_schema};")
        try:
            with orm_conn:
                cur = orm_conn.execute(stmt)
                cur.row_factory = partial(
                    self.orm_table_spec.table_row_factory2, validation=False
                )
                yield from cur
        finally:
            orm_conn.execute(f"DETACH DATABASE {attached_db_schema};")


class FileTableRegularORMPool(ORMThreadPoolBase[FileTableRegularFiles]):

    _orm_table_name = FT_REGULAR_TABLE_NAME


class FileTableNonRegularORM(ORMBase[FileTableNonRegularFiles]):

    _orm_table_name = FT_NON_REGULAR_TABLE_NAME


class FileTableDirORM(ORMBase[FileTableDirectories]):

    _orm_table_name = FT_DIR_TABLE_NAME


class FileTableDirORMPool(ORMThreadPoolBase[FileTableDirectories]):

    _orm_table_name = FT_DIR_TABLE_NAME
