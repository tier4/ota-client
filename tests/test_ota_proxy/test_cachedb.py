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
import pytest
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple
from otaclient.ota_proxy.ota_cache import CacheMeta, OTACacheDB
from otaclient.ota_proxy import config as cfg

logger = logging.getLogger(__name__)


class TestORM:
    @pytest.fixture(autouse=True)
    def create_table_defs(self):
        from otaclient.ota_proxy._orm import ORMBase, ColumnDescriptor, NULL_TYPE

        @dataclass
        class TableCls(ORMBase):
            str_field: ColumnDescriptor[str] = ColumnDescriptor(
                0,
                str,
                "TEXT",
                "UNIQUE",
                "NOT NULL",
                "PRIMARY KEY",
                default="invalid_url",
            )
            int_field: ColumnDescriptor[int] = ColumnDescriptor(
                1, int, "INTEGER", "NOT NULL", type_guard=(int, float)
            )
            float_field: ColumnDescriptor[float] = ColumnDescriptor(
                2, float, "INTEGER", "NOT NULL", type_guard=(int, float)
            )
            op_str_field: ColumnDescriptor[str] = ColumnDescriptor(3, str, "TEXT")
            op_int_field: ColumnDescriptor[int] = ColumnDescriptor(
                4, int, "INTEGER", type_guard=(int, float)
            )
            null_field: ColumnDescriptor[NULL_TYPE] = ColumnDescriptor(
                5, NULL_TYPE, "NULL"
            )

        self.table_cls = TableCls

    @pytest.mark.parametrize(
        "row, as_dict, as_tuple",
        (
            (
                {
                    "str_field": "unique_str",
                    "int_field": 123.9,
                    "float_field": 456,
                    "null_field": "not null",
                },
                {
                    "str_field": "unique_str",
                    "int_field": 123,
                    "float_field": 456,
                    "op_str_field": "",
                    "op_int_field": 0,
                    "null_field": None,
                },
                ("unique_str", 123, 456, "", 0, None),
            ),
        ),
    )
    def test_parse_and_export(self, row, as_dict: Dict[str, Any], as_tuple: Tuple[Any]):
        table_cls = self.table_cls
        parsed = table_cls.row_to_meta(row)
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
    @pytest.fixture(scope="class")
    def setup_db(self):
        """Setup a new conn to a in-memory otacache_db."""
        try:
            ota_cache_db = OTACacheDB(":memory:", init=True)
            time.sleep(0.5)
            yield ota_cache_db
        finally:
            ota_cache_db.close()

    @pytest.fixture(scope="class")
    def prepare_entries(self):

        entries: List[CacheMeta] = []
        for target_size, rotate_num in cfg.BUCKET_FILE_SIZE_DICT.items():
            for _i in range(rotate_num):
                entries.append(
                    CacheMeta(
                        url=f"{target_size}#{_i}",
                        bucket_idx=target_size,
                        size=target_size,
                    )
                )
        return entries

    @pytest.fixture(autouse=True)
    def setup_test(self, setup_db, prepare_entries):
        self.ota_cache_db: OTACacheDB = setup_db
        self.entries: List[CacheMeta] = prepare_entries

    def test_insert(self):
        """
        insert all prepared entries into the database
        """
        assert self.ota_cache_db.insert_entry(*self.entries) == len(self.entries)

    def test_lookup_all(self):
        """
        NOTE: the timestamp update is only executed at lookup method
        """
        entries_set = set(self.entries)
        checked_entries = self.ota_cache_db.lookup_all()
        assert entries_set == set(checked_entries)

    def test_lookup(self):
        """
        lookup the one entry in the database, and ensure the timestamp is updated
        """
        target = self.entries[-1]
        # lookup once to update last_acess
        checked_entry = self.ota_cache_db.lookup_entry(CacheMeta.url, target.url)
        assert checked_entry == target
        checked_entry = self.ota_cache_db.lookup_entry(CacheMeta.url, target.url)
        assert checked_entry and checked_entry.last_access > target.last_access

    def test_delete(self):
        """
        delete the whole 8MiB bucket
        """
        bucket_size = 8 * (1024**2)
        assert (
            self.ota_cache_db.remove_entries(CacheMeta.bucket_idx, bucket_size)
            == cfg.BUCKET_FILE_SIZE_DICT[bucket_size]
        )
        assert (
            len(self.ota_cache_db.lookup_all())
            == len(self.entries) - cfg.BUCKET_FILE_SIZE_DICT[bucket_size]
        )
