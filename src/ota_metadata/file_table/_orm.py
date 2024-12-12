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
from typing import Any, Callable, ClassVar, Literal, TypeVar

from simple_sqlite3_orm import ORMBase, ORMThreadPoolBase, TableSpec

from ._table import FileTableNonRegularFiles, FileTableRegularFiles

TableSpecType = TypeVar("TableSpecType", bound=TableSpec)


class FileTableRegularFilesORM(ORMBase[FileTableRegularFiles]):

    table_name: ClassVar[Literal["ft_regular"]] = "ft_regular"

    def __init__(
        self,
        con: Connection,
        schema_name: str | None | Literal["temp"] = None,
    ) -> None:
        super().__init__(con, table_name=self.table_name, schema_name=schema_name)


class FTRegularORMThreadPool(ORMThreadPoolBase[FileTableRegularFiles]):

    table_name: ClassVar[Literal["ft_regular"]] = "ft_regular"

    def __init__(
        self,
        schema_name: str | None = None,
        *,
        con_factory: Callable[[], Connection],
        number_of_cons: int,
        thread_name_prefix: str = "",
    ) -> None:
        super().__init__(
            table_name=self.table_name,
            schema_name=schema_name,
            con_factory=con_factory,
            number_of_cons=number_of_cons,
            thread_name_prefix=thread_name_prefix,
        )

    def check_entry(self, **kv: Any) -> bool:
        """A quick method to check if an entry exists."""
        _sql_stmt = self.orm_table_spec.table_select_stmt(
            select_from=self.orm_table_name,
            select_cols="*",
            function="count",
            where_cols=tuple(kv),
        )

        def _inner():
            with self._con as conn:
                _cur = conn.execute(_sql_stmt, kv)
                _cur.row_factory = None
                _res: tuple[int] = _cur.fetchone()
                return _res[0] > 0

        return self._pool.submit(_inner).result()


class FileTableNonRegularFilesORM(ORMBase[FileTableNonRegularFiles]):

    table_name: ClassVar[Literal["ft_non_regular"]] = "ft_non_regular"

    def __init__(
        self,
        con: Connection,
        schema_name: str | None | Literal["temp"] = None,
    ) -> None:
        super().__init__(con, table_name=self.table_name, schema_name=schema_name)
