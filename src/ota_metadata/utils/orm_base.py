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

from sqlite3 import Connection
from typing import Any, Callable, ClassVar, Literal

from simple_sqlite3_orm import ORMBase as _ORMBase
from simple_sqlite3_orm import ORMThreadPoolBase, TableSpecType


class ORMPoolBase(ORMThreadPoolBase[TableSpecType]):

    table_name: ClassVar[Any]

    def __init__(
        self,
        schema_name: str | None = None,
        *,
        con_factory: Callable[[], Connection],
        number_of_cons: int,
        thread_name_prefix: str = "",
    ) -> None:
        super().__init__(
            self.table_name,
            schema_name=schema_name,
            con_factory=con_factory,
            number_of_cons=number_of_cons,
            thread_name_prefix=thread_name_prefix,
        )

    def check_entry(self, col_to_return: str, **kv: Any) -> Any:
        """A quick method to check if an entry exists, and return the corresponding
        col of the first result.
        """
        _sql_stmt = self.orm_table_spec.table_select_stmt(
            select_from=self.orm_table_name,
            select_cols=(col_to_return,),
            where_cols=tuple(kv),
        )

        def _inner():
            with self._con as conn:
                _cur = conn.execute(_sql_stmt, kv)
                _cur.row_factory = None
                _res = _cur.fetchone()

                if _res:
                    return _res[0]

        return self._pool.submit(_inner).result()

    def try_remove_entry(self, **kv: Any) -> int:
        _res = self.orm_delete_entries(**kv).result()
        # NOTE: return value must be int, as the return must be int as
        #   no returning_stmt is provided.
        return _res  # type: ignore


class ORMBase(_ORMBase[TableSpecType]):

    table_name: ClassVar[Any]

    def __init__(
        self,
        con: Connection,
        schema_name: str | None | Literal["temp"] = None,
    ) -> None:
        super().__init__(con, self.table_name, schema_name=schema_name)

    def check_entry(self, col_to_return: str, **kv: Any) -> Any:
        """A quick method to check if an entry exists, and return the corresponding
        col of the first result.
        """
        _sql_stmt = self.orm_table_spec.table_select_stmt(
            select_from=self.orm_table_name,
            select_cols=(col_to_return,),
            where_cols=tuple(kv),
        )

        with self._con as conn:
            _cur = conn.execute(_sql_stmt, kv)
            _cur.row_factory = None
            _res = _cur.fetchone()

            if _res:
                return _res[0]
