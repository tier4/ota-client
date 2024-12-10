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
    """
    _pagination_stmt = self.orm_table_spec.table_select_stmt(
        select_from=self.orm_table_name,
        where_stmt="WHERE rowid > :not_before",
        limit=batch_size,
    )

    for _batch_cnt in count():
        _batch_empty = True

        for _entry in self.orm_execute(
            _pagination_stmt, params={"not_before": _batch_cnt * batch_size}
        ):
            _batch_empty = False
            yield _entry
        if _batch_empty:
            return


def iter_all_with_shuffle(
    self: ORMBase[TableSpecType], *, batch_size: int
) -> Generator[TableSpecType, None, None]:
    """Iter all entries with seek method by rowid, shuffle each batch before yield.

    NOTE: the target table must has rowid defined!
    """
    _pagination_stmt = self.orm_table_spec.table_select_stmt(
        select_from=self.orm_table_name,
        where_stmt="WHERE rowid > :not_before",
        limit=batch_size,
    )

    for _batch_cnt in count():
        _batch, _batch_empty = [], True

        for _entry in self.orm_execute(
            _pagination_stmt, params={"not_before": _batch_cnt * batch_size}
        ):
            _batch_empty = False
            _batch.append(_entry)
        if _batch_empty:
            return

        random.shuffle(_batch)
        yield from _batch