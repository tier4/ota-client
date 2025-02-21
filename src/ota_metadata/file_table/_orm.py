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

import logging
import sqlite3
from typing import Generator

from simple_sqlite3_orm import CreateIndexParams, ORMBase, ORMThreadPoolBase
from simple_sqlite3_orm.utils import wrap_value

from otaclient_common import EMPTY_FILE_SHA256_BYTE

from ._table import (
    FileTableDirectories,
    FileTableNonRegularFiles,
    FileTableRegularFiles,
)

logger = logging.getLogger(__name__)

FT_REGULAR_TABLE_NAME = "ft_regular"
FT_NON_REGULAR_TABLE_NAME = "ft_non_regular"
FT_DIR_TABLE_NAME = "ft_dir"
MAX_ENTRIES_PER_DIGEST = 10


class FileTableRegularORM(ORMBase[FileTableRegularFiles]):

    orm_bootstrap_table_name = FT_REGULAR_TABLE_NAME
    orm_bootstrap_indexes_params = [
        CreateIndexParams(index_name="digest_index", index_cols=("digest",))
    ]

    def iter_common_by_digest(
        self, other_db: str, *, max_entries_per_digest: int = MAX_ENTRIES_PER_DIGEST
    ) -> Generator[FileTableRegularFiles]:
        """Yield entries from <other_db>.ft_table which digest presented in this ft.

        This is for assisting faster delta_calculation without full disk scan.
        """

        attached_db_schema = "base"
        if sqlite3.sqlite_version_info < (3, 25, 0):
            logger.warning(
                f"detect {sqlite3.sqlite_version_info=} < 3.25, use fallback query"
            )

            stmt = f"""\
            SELECT d2.*
            FROM base.{FT_REGULAR_TABLE_NAME} AS d2
            INNER JOIN (
                SELECT digest
                FROM main.{FT_REGULAR_TABLE_NAME}
                WHERE digest != {wrap_value(EMPTY_FILE_SHA256_BYTE)}
                GROUP BY digest
            ) AS d1 USING(digest) ORDER BY digest;
            """
        else:
            stmt = f"""\
            WITH ranked AS (
                SELECT d2.*, ROW_NUMBER() OVER (PARTITION BY d2.digest) AS rown
                FROM base.{FT_REGULAR_TABLE_NAME} AS d2
                INNER JOIN (
                    SELECT digest
                    FROM main.{FT_REGULAR_TABLE_NAME}
                    WHERE digest != {wrap_value(EMPTY_FILE_SHA256_BYTE)}
                    GROUP BY digest
                ) AS d1 USING(digest) ORDER BY digest
            )
            SELECT * FROM ranked WHERE rown <= {max_entries_per_digest};
            """
        orm_conn = self.orm_con

        orm_conn.execute(f"ATTACH '{other_db}' AS {attached_db_schema};")
        try:
            with orm_conn:
                cur = orm_conn.execute(stmt)
                cur.row_factory = self.orm_table_spec.table_row_factory2
                yield from cur
        finally:
            orm_conn.execute(f"DETACH DATABASE {attached_db_schema};")


class FileTableRegularORMPool(ORMThreadPoolBase[FileTableRegularFiles]):

    orm_bootstrap_table_name = FT_REGULAR_TABLE_NAME


class FileTableNonRegularORM(ORMBase[FileTableNonRegularFiles]):

    orm_bootstrap_table_name = FT_NON_REGULAR_TABLE_NAME


class FileTableDirORM(ORMBase[FileTableDirectories]):

    orm_bootstrap_table_name = FT_DIR_TABLE_NAME


class FileTableDirORMPool(ORMThreadPoolBase[FileTableDirectories]):

    orm_bootstrap_table_name = FT_DIR_TABLE_NAME
