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

import sqlite3

from simple_sqlite3_orm.utils import check_db_integrity, lookup_table

from ota_metadata.metadata_store.orm import DirectoriesORM, RegularFilesORM, SymlinksORM

DIR_TABLE_NAME = "dir_table"
SYMLINK_TABLE_NAME = "symlink_table"
REGULARFILE_TABLE_NAME = "regular_file_table"


def init_filetable_db(
    conn: sqlite3.Connection, *, schema_name: str | None = None
) -> None:
    dir_orm = DirectoriesORM(conn, DIR_TABLE_NAME, schema_name=schema_name)
    dir_orm.orm_create_table()

    symlink_orm = SymlinksORM(conn, SYMLINK_TABLE_NAME, schema_name=schema_name)
    symlink_orm.orm_create_table()

    regularfile_orm = RegularFilesORM(
        conn, REGULARFILE_TABLE_NAME, schema_name=schema_name
    )
    regularfile_orm.orm_create_table()
    regularfile_orm.orm_create_index(index_name="digest_idx", index_keys=("digest",))


def check_filetable_db(conn: sqlite3.Connection) -> bool:
    db_normal = check_db_integrity(conn)
    tables_presented = (
        lookup_table(conn, DIR_TABLE_NAME)
        and lookup_table(conn, SYMLINK_TABLE_NAME)
        and lookup_table(conn, REGULARFILE_TABLE_NAME)
    )
    return db_normal and tables_presented
