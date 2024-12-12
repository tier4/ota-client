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
from typing import Literal, TypeVar

from simple_sqlite3_orm import ORMBase, ORMThreadPoolBase, TableSpec

from ..utils.sqlite3_helper import iter_all
from ._table import FileTableNonRegularFiles, FileTableRegularFiles

TableSpecType = TypeVar("TableSpecType", bound=TableSpec)


class FileTableRegularFilesORM(ORMBase[FileTableRegularFiles]):
    def __init__(
        self,
        con: Connection,
        schema_name: str | None | Literal["temp"] = None,
    ) -> None:
        super().__init__(
            con, table_name=FileTableRegularFiles.table_name, schema_name=schema_name
        )

    iter_all = iter_all


class FTRegularORMThreadPool(ORMThreadPoolBase[FileTableRegularFiles]): ...


class FileTableNonRegularFilesORM(ORMBase[FileTableNonRegularFiles]):
    def __init__(
        self,
        con: Connection,
        schema_name: str | None | Literal["temp"] = None,
    ) -> None:
        super().__init__(
            con, table_name=FileTableNonRegularFiles.table_name, schema_name=schema_name
        )

    iter_all = iter_all
