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
from typing import Callable, Literal

from simple_sqlite3_orm import ORMBase, ORMThreadPoolBase

from ._table import FileSystemTable


class FileSystemTableORM(ORMBase[FileSystemTable]):
    def __init__(
        self,
        con: Connection,
        schema_name: str | None | Literal["temp"] = None,
    ) -> None:
        super().__init__(
            con, table_name=FileSystemTable.table_name, schema_name=schema_name
        )


class FileSystemTableORMThreadPool(ORMThreadPoolBase[FileSystemTable]):
    def __init__(
        self,
        schema_name: str | None = None,
        *,
        con_factory: Callable[[], Connection],
        number_of_cons: int,
        thread_name_prefix: str = "",
    ) -> None:
        super().__init__(
            FileSystemTable.table_name,
            schema_name,
            con_factory=con_factory,
            number_of_cons=number_of_cons,
            thread_name_prefix=thread_name_prefix,
        )
