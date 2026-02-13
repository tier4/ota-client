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
from contextlib import closing
from pathlib import Path

import pytest
from ota_image_libs.v1.file_table.db import (
    FileTableDirORM,
    FileTableInodeORM,
    FileTableNonRegularORM,
    FileTableRegularORM,
    FileTableResourceORM,
)
from ota_image_libs.v1.file_table.schema import (
    FileTableDirectoryTypedDict,
    FiletableInodeTypedDict,
    FileTableNonRegularFiles,
    FileTableNonRegularTypedDict,
    FileTableRegularTypedDict,
)

from ota_metadata.legacy2.csv_parser import (
    parse_create_file_table_row,
    parse_dirs_csv_line,
    parse_dirs_from_csv_file,
    parse_persists_csv_line,
    parse_regular_csv_line,
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
    "_input, _ft_expected, _ft_inode_expected, _rs_expected",
    (
        # rev4: mode,uid,gid,link number,sha256sum,'path/to/file'[,size[,inode,[compression_alg]]]
        (
            r"0644,1000,1000,3,0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef,'/aaa\,'\'',Ελληνικό/to/file',1234,12345678,zst",
            # NOTE: resource_id and inode_id are assigned automatically when inserting into the database
            FileTableRegularTypedDict(
                path=r"/aaa\,',Ελληνικό/to/file",
            ),
            # NOTE: otaclient implementation details:
            #   TO label the hardlinked file entry, and indicate database builder to
            #       not automatically assign an inode_id for hardlinked entry, pre-assign inode_id
            #       by `-<inode_id_in_csv_entry>`.
            #   Later the database builder will check against the inode_id, and calculating
            #       links_count field for hardlinked file entry.
            FiletableInodeTypedDict(
                mode=int("0644", 8) | stat.S_IFREG,
                uid=1000,
                gid=1000,
                inode_id=-12345678,
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
            FileTableRegularTypedDict(
                path=r"/aaa\,',Ελληνικό/to/file",
            ),
            FiletableInodeTypedDict(
                mode=int("0644", 8) | stat.S_IFREG,
                uid=1000,
                gid=1000,
                inode_id=-12345678,
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
            FileTableRegularTypedDict(
                path=r"/aaa,',Ελληνικό/to/file",
            ),
            FiletableInodeTypedDict(
                mode=int("0644", 8) | stat.S_IFREG,
                uid=1000,
                gid=1000,
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
    _input: str,
    _ft_expected: FileTableRegularTypedDict,
    _ft_inode_expected: FiletableInodeTypedDict,
    _rs_expected: ResourceTable,
):
    _raw_parsed = parse_regular_csv_line(_input)

    _ft, _ft_inode, _rs = parse_create_file_table_row(_raw_parsed)
    assert _ft == _ft_expected
    assert _ft_inode == _ft_inode_expected
    assert _rs == _rs_expected


@pytest.mark.parametrize(
    "_input, _inode_id, _expected, _expected_inode",
    (
        (
            r"0755,0,0,'/usr/lib/python3/aaa,'\''Ελληνικό'",
            _inode_id := 12345,
            FileTableDirectoryTypedDict(
                path=r"/usr/lib/python3/aaa,'Ελληνικό",
                inode_id=_inode_id,
            ),
            FiletableInodeTypedDict(
                mode=int("0755", 8) | stat.S_IFDIR,
                uid=0,
                gid=0,
                inode_id=_inode_id,
            ),
        ),
    ),
)
def test_dirs_txt(
    _input: str,
    _inode_id: int,
    _expected: FileTableDirectoryTypedDict,
    _expected_inode: FiletableInodeTypedDict,
):
    assert parse_dirs_csv_line(_input, _inode_id) == (_expected, _expected_inode)


@pytest.mark.parametrize(
    "_input, _inode_id, _expected, _expected_inode",
    (
        # ensure ' are escaped and (,') will not break path parsing
        (
            r"0777,0,0,'/var/lib/ieee-data/iab.csv','../../ieee-data/'\'','\''Ελληνικό.csv'",
            _inode_id := 123456,
            FileTableNonRegularTypedDict(
                path=r"/var/lib/ieee-data/iab.csv",
                inode_id=_inode_id,
                meta=r"../../ieee-data/','Ελληνικό.csv".encode(),
            ),
            FiletableInodeTypedDict(
                mode=int("0777", 8) | stat.S_IFLNK,
                uid=0,
                gid=0,
                inode_id=_inode_id,
            ),
        ),
    ),
)
def test_symlinks_txt(
    _input: str,
    _inode_id: int,
    _expected: FileTableNonRegularFiles,
    _expected_inode: FiletableInodeTypedDict,
):
    assert parse_symlinks_csv_line(_input, _inode_id) == (_expected, _expected_inode)


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


def test_parse_and_build_file_table_db_from_csv(tmp_path: Path):
    _ft_db = tmp_path / "file_table.sqlite3"
    _rs_db = tmp_path / "resource_table.sqlite3"

    with (
        closing(sqlite3.connect(_ft_db)) as fst_conn,
        closing(sqlite3.connect(_rs_db)) as rst_conn,
    ):
        ft_regular_orm = FileTableRegularORM(fst_conn)
        ft_regular_orm.orm_bootstrap_db()
        ft_dir_orm = FileTableDirORM(fst_conn)
        ft_dir_orm.orm_bootstrap_db()
        ft_non_regular_orm = FileTableNonRegularORM(fst_conn)
        ft_non_regular_orm.orm_bootstrap_db()
        ft_resource_orm = FileTableResourceORM(fst_conn)
        ft_resource_orm.orm_bootstrap_db()
        ft_inode_orm = FileTableInodeORM(fst_conn)
        ft_inode_orm.orm_bootstrap_db()
        rs_orm = ResourceTableORM(rst_conn)
        rs_orm.orm_bootstrap_db()

        inode_start = 1
        total_regulars_num, inode_start = parse_regulars_from_csv_file(
            _fpath=regulars_txt,
            _orm=ft_regular_orm,
            _orm_ft_resource=ft_resource_orm,
            _orm_rs=rs_orm,
            _orm_inode=ft_inode_orm,
            inode_start=inode_start,
        )
        logger.info(f"{total_regulars_num=}, {inode_start=}")
        assert total_regulars_num > 0

        dirs_num, inode_start = parse_dirs_from_csv_file(
            dirs_txt,
            ft_dir_orm,
            _inode_orm=ft_inode_orm,
            inode_start=inode_start,
        )
        logger.info(f"{dirs_num=}, {inode_start=}")
        assert dirs_num > 0

        symlinks_num, _ = parse_symlinks_from_csv_file(
            symlinks_txt,
            ft_non_regular_orm,
            _inode_orm=ft_inode_orm,
            inode_start=inode_start,
        )
        logger.info(f"{symlinks_num=}, {inode_start=}")
        assert symlinks_num > 0

        assert inode_start > 1
