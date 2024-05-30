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


import logging
import sqlite3
from dataclasses import dataclass
from os import urandom
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest

from ota_proxy import config as cfg
from ota_proxy.orm import NULL_TYPE
from ota_proxy.ota_cache import CacheMeta, OTACacheDB
from ota_proxy.utils import url_based_hash

logger = logging.getLogger(__name__)


class TestORM:
    @pytest.fixture(autouse=True)
    def create_table_defs(self):
        from ota_proxy.orm import NULL_TYPE, ColumnDescriptor, ORMBase

        @dataclass
        class TableCls(ORMBase):
            str_field: ColumnDescriptor[str] = ColumnDescriptor(
                str,
                "TEXT",
                "UNIQUE",
                "NOT NULL",
                "PRIMARY KEY",
                default="invalid_url",
            )
            int_field: ColumnDescriptor[int] = ColumnDescriptor(
                int, "INTEGER", "NOT NULL", type_guard=(int, float)
            )
            float_field: ColumnDescriptor[float] = ColumnDescriptor(
                float, "INTEGER", "NOT NULL", type_guard=(int, float)
            )
            op_str_field: ColumnDescriptor[str] = ColumnDescriptor(str, "TEXT")
            op_int_field: ColumnDescriptor[int] = ColumnDescriptor(
                int, "INTEGER", type_guard=(int, float)
            )
            null_field: ColumnDescriptor[NULL_TYPE] = ColumnDescriptor(
                NULL_TYPE, "NULL"
            )

        self.table_cls = TableCls

    @pytest.mark.parametrize(
        "raw_row, as_dict, as_tuple",
        (
            (
                {
                    "str_field": "unique_str",
                    "int_field": 123.123,  # expect to be converted into int
                    "float_field": 456.123,
                    "null_field": "should_not_be_set",
                },
                {
                    "str_field": "unique_str",
                    "int_field": 123,
                    "float_field": 456.123,
                    "op_str_field": "",
                    "op_int_field": 0,
                    "null_field": NULL_TYPE(),
                },
                ("unique_str", 123, 456.123, "", 0, NULL_TYPE()),
            ),
        ),
    )
    def test_parse_and_export(
        self, raw_row, as_dict: Dict[str, Any], as_tuple: Tuple[Any]
    ):
        table_cls = self.table_cls
        parsed = table_cls.row_to_meta(raw_row)
        assert parsed.asdict() == as_dict
        assert parsed.astuple() == as_tuple
        assert table_cls.row_to_meta(parsed.astuple()).asdict() == as_dict

    @pytest.mark.parametrize(
        "table_name, expected",
        (
            (
                "table_name",
                (
                    "CREATE TABLE table_name("
                    "str_field TEXT UNIQUE NOT NULL PRIMARY KEY, "
                    "int_field INTEGER NOT NULL, "
                    "float_field INTEGER NOT NULL, "
                    "op_str_field TEXT, "
                    "op_int_field INTEGER, "
                    "null_field NULL)"
                ),
            ),
        ),
    )
    def test_get_create_table_stmt(self, table_name: str, expected: str):
        assert self.table_cls.get_create_table_stmt(table_name) == expected

    @pytest.mark.parametrize(
        "name",
        (
            "str_field",
            "int_field",
            "float_field",
            "op_str_field",
            "op_int_field",
            "null_field",
        ),
    )
    def test_contains_field(self, name: str):
        table_cls = self.table_cls
        assert (col_descriptor := getattr(table_cls, name))
        assert table_cls.contains_field(name)
        assert col_descriptor is table_cls.__dict__[name]
        assert col_descriptor and table_cls.contains_field(col_descriptor)

    def test_type_check(self):
        inst = self.table_cls()
        # field float_field is type_checked
        inst.float_field = 123.456
        with pytest.raises(TypeError):
            inst.float_field = "str_type"

    @pytest.mark.parametrize(
        "row_dict",
        (
            {
                "str_field": "unique_str",
                "int_field": 123.9,
                "float_field": 456,
            },
        ),
    )
    def test_with_actual_db(self, row_dict: Dict[str, Any]):
        """Setup a new conn to a in-memory otacache_db."""
        table_cls, table_name = self.table_cls, "test_table"
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        with conn:  # test create table with table_cls
            conn.execute(table_cls.get_create_table_stmt(table_name))

        row_inst = table_cls.row_to_meta(row_dict)
        logger.info(row_inst)
        with conn:  # insert one entry
            assert (
                conn.execute(
                    f"INSERT INTO {table_name} VALUES ({table_cls.get_shape()})",
                    row_inst.astuple(),
                ).rowcount
                == 1
            )
        with conn:  # get the entry back
            cur = conn.execute(f"SELECT * from {table_name}", ())
            row_parsed = table_cls.row_to_meta(cur.fetchone())
            assert row_parsed == row_inst
            assert row_parsed.asdict() == row_inst.asdict()

        conn.close()


class TestOTACacheDB:
    @pytest.fixture(autouse=True)
    def prepare_db(self, tmp_path: Path):
        self.db_f = db_f = tmp_path / "db_f"
        self.entries = entries = []

        # prepare data
        for target_size, rotate_num in cfg.BUCKET_FILE_SIZE_DICT.items():
            for _i in range(rotate_num):
                mocked_url = f"{target_size}#{_i}"
                entries.append(
                    CacheMeta(
                        file_sha256=url_based_hash(mocked_url),
                        url=mocked_url,
                        bucket_idx=target_size,
                        cache_size=target_size,
                    )
                )
        # insert entry into db
        OTACacheDB.init_db_file(db_f)
        with OTACacheDB(db_f) as db_conn:
            db_conn.insert_entry(*self.entries)

        # prepare connection
        try:
            self.conn = OTACacheDB(self.db_f)
            yield
        finally:
            self.conn.close()

    def test_insert(self, tmp_path: Path):
        db_f = tmp_path / "db_f2"
        # first insert
        OTACacheDB.init_db_file(db_f)
        with OTACacheDB(db_f) as db_conn:
            assert db_conn.insert_entry(*self.entries) == len(self.entries)
        # check the insertion with another connection
        with OTACacheDB(db_f) as db_conn:
            _all = db_conn.lookup_all()
            # should have the same order as insertion
            assert _all == self.entries

    def test_db_corruption(self, tmp_path: Path):
        test_db_f = tmp_path / "corrupted_db_f"
        # intensionally create corrupted db file
        with open(self.db_f, "rb") as src, open(test_db_f, "wb") as dst:
            count = 0
            while data := src.read(32):
                count += 1
                dst.write(data)
                if count % 2 == 0:
                    dst.write(urandom(8))

        assert not OTACacheDB.check_db_file(test_db_f)

    def test_lookup_all(self):
        """
        NOTE: the timestamp update is only executed at lookup method
        """
        entries_set = set(self.entries)
        checked_entries = self.conn.lookup_all()
        assert entries_set == set(checked_entries)

    def test_lookup(self):
        """
        lookup the one entry in the database, and ensure the timestamp is updated
        """
        target = self.entries[-1]
        # lookup once to update last_acess
        checked_entry = self.conn.lookup_entry(CacheMeta.url, target.url)
        assert checked_entry == target
        checked_entry = self.conn.lookup_entry(CacheMeta.url, target.url)
        assert checked_entry and checked_entry.last_access > target.last_access

    def test_delete(self):
        """
        delete the whole 8MiB bucket
        """
        bucket_size = 8 * (1024**2)
        assert (
            self.conn.remove_entries(CacheMeta.bucket_idx, bucket_size)
            == cfg.BUCKET_FILE_SIZE_DICT[bucket_size]
        )
        assert (
            len(self.conn.lookup_all())
            == len(self.entries) - cfg.BUCKET_FILE_SIZE_DICT[bucket_size]
        )
