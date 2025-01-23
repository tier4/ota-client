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
"""
NOTE: the test cases are mostly re-used from the previous implementation.
"""


from __future__ import annotations

import logging
import sqlite3
import stat
from pathlib import Path

import pytest

from ota_metadata.file_table import (
    FileEntryAttrs,
    FileTableDirectories,
    FileTableDirORM,
    FileTableNonRegularFiles,
    FileTableNonRegularORM,
    FileTableRegularFiles,
    FileTableRegularORM,
)
from ota_metadata.legacy2.csv_parser import (
    parse_dirs_csv_line,
    parse_dirs_from_csv_file,
    parse_persists_csv_line,
    parse_regulars_csv_line,
    parse_regulars_from_csv_file,
    parse_symlinks_csv_line,
    parse_symlinks_from_csv_file,
)
from ota_metadata.legacy2.rs_table import ResourceTable, ResourceTableORM
from tests.conftest import TestConfiguration as test_cfg

logger = logging.getLogger(__name__)

OTA_IMAGE_ROOT = Path(test_cfg.OTA_IMAGE_DIR)

#
# ------ test CSV line parser ------ #
#


# try to include as any special characters as possible
@pytest.mark.parametrize(
    "_input, _ft_expected, _rs_expected",
    (
        # rev4: mode,uid,gid,link number,sha256sum,'path/to/file'[,size[,inode,[compression_alg]]]
        (
            r"0644,1000,1000,3,0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef,'/aaa\,'\'',Ελληνικό/to/file',1234,12345678,zst",
            FileTableRegularFiles(
                path=r"/aaa\,',Ελληνικό/to/file",
                digest=bytes.fromhex(
                    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
                ),
                entry_attrs=FileEntryAttrs(
                    mode=int("0644", 8) | stat.S_IFREG,
                    uid=1000,
                    gid=1000,
                    size=1234,
                    inode=12345678,
                ),
            ),
            ResourceTable(
                digest=bytes.fromhex(
                    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
                ),
                original_size=1234,
                compression_alg="zst",
            ),
        ),
        # rev3: mode,uid,gid,link number,sha256sum,'path/to/file'[,size[,inode]]
        (
            r"0644,1000,1000,3,0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef,'/aaa\,'\'',Ελληνικό/to/file',1234,12345678,",
            FileTableRegularFiles(
                path=r"/aaa\,',Ελληνικό/to/file",
                digest=bytes.fromhex(
                    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
                ),
                entry_attrs=FileEntryAttrs(
                    mode=int("0644", 8) | stat.S_IFREG,
                    uid=1000,
                    gid=1000,
                    size=1234,
                    inode=12345678,
                ),
            ),
            ResourceTable(
                path=r"/aaa\,',Ελληνικό/to/file",
                digest=bytes.fromhex(
                    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
                ),
                original_size=1234,
            ),
        ),
        # rev2: mode,uid,gid,link number,sha256sum,'path/to/file'[,size]
        (
            r"0644,1000,1000,1,0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef,'/aaa,'\'',Ελληνικό/to/file',1234",
            FileTableRegularFiles(
                path=r"/aaa,',Ελληνικό/to/file",
                digest=bytes.fromhex(
                    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
                ),
                entry_attrs=FileEntryAttrs(
                    mode=int("0644", 8) | stat.S_IFREG,
                    uid=1000,
                    gid=1000,
                    size=1234,
                ),
            ),
            ResourceTable(
                path=r"/aaa,',Ελληνικό/to/file",
                digest=bytes.fromhex(
                    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
                ),
                original_size=1234,
            ),
        ),
        # rev1: mode,uid,gid,link number,sha256sum,'path/to/file'
        # NOTE: rev1 is not supported anymore, the size field is MUST now.
    ),
)
def test_regulars_txt(
    _input: str, _ft_expected: FileTableRegularFiles, _rs_expected: ResourceTable
):
    _ft, _rs = parse_regulars_csv_line(_input)
    assert _ft == _ft_expected
    assert _rs == _rs_expected


@pytest.mark.parametrize(
    "_input, _expected",
    (
        (
            r"0755,0,0,'/usr/lib/python3/aaa,'\''Ελληνικό'",
            FileTableDirectories(
                path=r"/usr/lib/python3/aaa,'Ελληνικό",
                entry_attrs=FileEntryAttrs(
                    mode=int("0755", 8) | stat.S_IFDIR, uid=0, gid=0
                ),
            ),
        ),
    ),
)
def test_dirs_txt(_input: str, _expected: FileTableDirectories):
    assert parse_dirs_csv_line(_input) == _expected


@pytest.mark.parametrize(
    "_input, _expected",
    (
        # ensure ' are escaped and (,') will not break path parsing
        (
            r"0777,0,0,'/var/lib/ieee-data/iab.csv','../../ieee-data/'\'','\''Ελληνικό.csv'",
            FileTableNonRegularFiles(
                path=r"/var/lib/ieee-data/iab.csv",
                entry_attrs=FileEntryAttrs(
                    mode=int("0777", 8) | stat.S_IFLNK,
                    uid=0,
                    gid=0,
                ),
                contents=r"../../ieee-data/','Ελληνικό.csv".encode(),
            ),
        ),
    ),
)
def test_symlinks_txt(_input: str, _expected: FileTableNonRegularFiles):
    assert parse_symlinks_csv_line(_input) == _expected


@pytest.mark.parametrize(
    "_input, _expected",
    (
        (
            r"'/etc/net'\''plan'",
            r"/etc/net'plan",
        ),
    ),
)
def test_persistent_txt(_input: str, _expected: str):
    assert parse_persists_csv_line(_input) == _expected


#
# ------ test import sqlite3 database ------ #
#

regulars_txt = OTA_IMAGE_ROOT / "regulars.txt"
dirs_txt = OTA_IMAGE_ROOT / "dirs.txt"
symlinks_txt = OTA_IMAGE_ROOT / "symlinks.txt"


def test_parse_and_import_regulars_txt():
    with sqlite3.connect(
        "file:ft_table?mode=memory", uri=True
    ) as ft_table_conn, sqlite3.connect(
        "file:rs_table?mode=memory", uri=True
    ) as rs_table_conn:
        ft_table_orm = FileTableRegularORM(ft_table_conn)
        ft_table_orm.orm_create_table()

        rs_table_orm = ResourceTableORM(rs_table_conn)
        rs_table_orm.orm_create_table()
        _imported = parse_regulars_from_csv_file(
            regulars_txt, ft_table_orm, rs_table_orm, cleanup=False
        )
        logger.info(f"imported {_imported} entries")
        assert _imported > 0


def test_parse_and_import_dirs_txt():
    with sqlite3.connect(":memory:") as conn:
        orm = FileTableDirORM(conn)
        orm.orm_create_table()
        _imported = parse_dirs_from_csv_file(dirs_txt, orm, cleanup=False)
        logger.info(f"imported {_imported} entries")
        assert _imported > 0


def test_parse_and_import_symlinks_txt():
    with sqlite3.connect(":memory:") as conn:
        orm = FileTableNonRegularORM(conn)
        orm.orm_create_table()
        _imported = parse_symlinks_from_csv_file(symlinks_txt, orm, cleanup=False)
        logger.info(f"imported {_imported} entries")
        assert _imported > 0
