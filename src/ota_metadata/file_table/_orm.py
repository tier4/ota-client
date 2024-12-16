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

from typing import ClassVar, Literal

from simple_sqlite3_orm import ORMBase, ORMThreadPoolBase

from ._table import (
    FileTableDirectories,
    FileTableNonRegularFiles,
    FileTableRegularFiles,
)


class FTRegularORM(ORMBase[FileTableRegularFiles]):

    table_name: ClassVar[Literal["ft_regular"]] = "ft_regular"


class FTRegularORMPool(ORMThreadPoolBase[FileTableRegularFiles]):

    table_name: ClassVar[Literal["ft_regular"]] = "ft_regular"


class FTNonRegularORM(ORMBase[FileTableNonRegularFiles]):

    table_name: ClassVar[Literal["ft_non_regular"]] = "ft_non_regular"


class FTDirORM(ORMBase[FileTableDirectories]):

    table_name: ClassVar[Literal["ft_dir"]] = "ft_dir"
