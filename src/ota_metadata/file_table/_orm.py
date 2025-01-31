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


class FileTableRegularORMPool(ORMThreadPoolBase[FileTableRegularFiles]):

    _orm_table_name = FT_REGULAR_TABLE_NAME


class FileTableNonRegularORM(ORMBase[FileTableNonRegularFiles]):

    _orm_table_name = FT_NON_REGULAR_TABLE_NAME


class FileTableNonRegularORMPool(ORMThreadPoolBase[FileTableNonRegularFiles]):

    _orm_table_name = FT_NON_REGULAR_TABLE_NAME


class FileTableDirORM(ORMBase[FileTableDirectories]):

    _orm_table_name = FT_DIR_TABLE_NAME


class FileTableDirORMPool(ORMThreadPoolBase[FileTableDirectories]):

    _orm_table_name = FT_DIR_TABLE_NAME
