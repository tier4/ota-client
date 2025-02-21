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
import textwrap
from typing import Any, Generator, NamedTuple

from simple_sqlite3_orm import CreateIndexParams, ORMBase, ORMThreadPoolBase
from simple_sqlite3_orm.utils import wrap_value
from typing_extensions import Self

from otaclient_common import EMPTY_FILE_SHA256_BYTE

from ._table import (
    FileTableDirectories,
    FileTableNonRegularFiles,
    FileTableRegularFiles,
    FileTableResource,
)

logger = logging.getLogger(__name__)

FT_REGULAR_TABLE_NAME = "ft_regular"
FT_NON_REGULAR_TABLE_NAME = "ft_non_regular"
FT_DIR_TABLE_NAME = "ft_dir"
FT_RESOURCE_TABLE_NAME = "ft_resource"
MAX_ENTRIES_PER_DIGEST = 10


class FileEntryToScan(NamedTuple):
    """A helper type for typing output result of iter_common_by_digest."""

    path: str
    digest: bytes
    size: int

    @classmethod
    def row_factory(cls, _cursor, _row: tuple[Any, ...] | Any) -> Self:
        return cls(*_row)


class FileTableRegularORM(ORMBase[FileTableRegularFiles]):

    orm_bootstrap_table_name = FT_REGULAR_TABLE_NAME
    orm_bootstrap_indexes_params = [
        CreateIndexParams(index_name="resource_id_index", index_cols=("resource_id",))
    ]

    def iter_common_by_digest(
        self, other_db: str, *, max_entries_per_digest: int = MAX_ENTRIES_PER_DIGEST
    ) -> Generator[FileEntryToScan]:
        """Yield entries from <other_db>.ft_table which digest presented in this ft.

        This is for assisting faster delta_calculation without full disk scan.
        """

        attached_db_schema = "base"
        if sqlite3.sqlite_version_info < (3, 25, 0):
            logger.warning(
                f"detect {sqlite3.sqlite_version_info=} < 3.25, use fallback query"
            )

            stmt = textwrap.dedent(
                f"""\
            WITH common_digests AS (
                SELECT db1.digest
                FROM {FT_RESOURCE_TABLE_NAME} AS db1
                INNER JOIN base.{FT_RESOURCE_TABLE_NAME} AS db2 USING(digest)
                WHERE digest != {wrap_value(EMPTY_FILE_SHA256_BYTE)}
            )
            SELECT {FT_REGULAR_TABLE_NAME}.path, {FT_RESOURCE_TABLE_NAME}.digest, {FT_RESOURCE_TABLE_NAME}.size
            FROM {FT_REGULAR_TABLE_NAME}
            JOIN {FT_RESOURCE_TABLE_NAME} ON {FT_REGULAR_TABLE_NAME}.resource_id = {FT_RESOURCE_TABLE_NAME}.resource_id
            JOIN common_digests ON {FT_RESOURCE_TABLE_NAME}.digest = common_digests.digest
            ORDER BY {FT_RESOURCE_TABLE_NAME}.digest;
            """
            )
        else:
            stmt = textwrap.dedent(
                f"""\
            WITH common_digests AS (
                SELECT db1.digest
                FROM {FT_RESOURCE_TABLE_NAME} AS db1
                INNER JOIN base.{FT_RESOURCE_TABLE_NAME} AS db2 USING(digest)
                WHERE digest != {wrap_value(EMPTY_FILE_SHA256_BYTE)}
            ), ranked_results AS (
                SELECT 
                    {FT_REGULAR_TABLE_NAME}.path, 
                    {FT_RESOURCE_TABLE_NAME}.digest, 
                    {FT_RESOURCE_TABLE_NAME}.size,
                    ROW_NUMBER() OVER (PARTITION BY {FT_RESOURCE_TABLE_NAME}.digest ORDER BY {FT_RESOURCE_TABLE_NAME}.path) AS row_num
                FROM {FT_REGULAR_TABLE_NAME}
                JOIN {FT_RESOURCE_TABLE_NAME} USING(resource_id)
                JOIN common_digests USING(digest)
            )

            SELECT path, digest, size
            FROM ranked_results
            WHERE row_num <= {max_entries_per_digest}
            ORDER BY digest;
            """
            )
        orm_conn = self.orm_con

        orm_conn.execute(f"ATTACH '{other_db}' AS {attached_db_schema};")
        try:
            with orm_conn:
                cur = orm_conn.execute(stmt)
                cur.row_factory = FileEntryToScan.row_factory
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


class FileTableResourceORM(ORMBase[FileTableResource]):

    orm_bootstrap_table_name = FT_RESOURCE_TABLE_NAME
    orm_bootstrap_indexes_params = [
        CreateIndexParams(index_name="digest_index", index_cols=("digest",))
    ]
