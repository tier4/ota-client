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

import random
from itertools import count
from typing import Generator, TypeVar

from simple_sqlite3_orm import ORMBase, TableSpec

TableSpecType = TypeVar("TableSpecType", bound=TableSpec)


def iter_all(
    self: ORMBase[TableSpecType], *, batch_size: int
) -> Generator[TableSpecType, None, None]:
    """Iter all entries with seek method by rowid.

    NOTE: the target table must has rowid defined!
    NOTE: the rowid MUST BE continues without any holes!
    """
    _pagination_stmt = self.orm_table_spec.table_select_stmt(
        select_from=self.orm_table_name,
        where_stmt="WHERE rowid > :not_before",
        limit=batch_size,
    )

    for _batch_cnt in count():
        _batch = self.orm_execute(
            _pagination_stmt, params={"not_before": _batch_cnt * batch_size}
        )
        if not _batch:
            return
        yield from _batch


def iter_all_with_shuffle(
    self: ORMBase[TableSpecType], *, batch_size: int
) -> Generator[TableSpecType, None, None]:
    """Iter all entries with seek method by rowid, shuffle each batch before yield.

    NOTE: the target table must has rowid defined!
    NOTE: the rowid MUST BE continues without any holes!
    """
    _pagination_stmt = self.orm_table_spec.table_select_stmt(
        select_from=self.orm_table_name,
        where_stmt="WHERE rowid > :not_before",
        limit=batch_size,
    )

    for _batch_cnt in count():
        _batch = self.orm_execute(
            _pagination_stmt, params={"not_before": _batch_cnt * batch_size}
        )
        if not _batch:
            return
        random.shuffle(_batch)
        yield from _batch


def sort_and_replace(_orm: ORMBase, table_name: str, *, order_by_col: str) -> None:
    """Sort the table, and then replace the old table with the sorted one."""
    ORIGINAL_TABLE_NAME = table_name
    SORTED_TABLE_NAME = f"{table_name}_sorted"
    _table_spec = _orm.orm_table_spec

    _table_create_stmt = _table_spec.table_create_stmt(SORTED_TABLE_NAME)
    _dump_sorted = (
        f"INSERT INTO {SORTED_TABLE_NAME} SELECT * FROM "
        f"{ORIGINAL_TABLE_NAME} ORDER BY {order_by_col};"
    )

    conn = _orm.orm_con
    with conn as conn:
        conn.executescript(
            "\n".join(
                [
                    "BEGIN;",
                    _table_create_stmt,
                    _dump_sorted,
                    f"DROP TABLE {ORIGINAL_TABLE_NAME};",
                    f"ALTER TABLE {SORTED_TABLE_NAME} RENAME TO {ORIGINAL_TABLE_NAME};",
                ]
            )
        )
    with conn as conn:
        conn.execute("VACUUM;")
