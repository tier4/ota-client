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

from simple_sqlite3_orm import ORMBase

from ota_metadata._file_table.tables import (
    DirectoriesTable,
    RegularFilesTable,
    SymlinksTable,
)


class DirectoriesORM(ORMBase[DirectoriesTable]): ...


class SymlinksORM(ORMBase[SymlinksTable]): ...


class RegularFilesORM(ORMBase[RegularFilesTable]): ...
