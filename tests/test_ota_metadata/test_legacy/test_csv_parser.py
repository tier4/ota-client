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

import stat

import pytest

from ota_metadata.file_table import (
    FileEntryAttrs,
    FileTableDirectories,
    FileTableNonRegularFiles,
    FileTableRegularFiles,
)
from ota_metadata.legacy.csv_parser import (
    parse_dirs_csv_line,
    parse_persists_csv_line,
    parse_regulars_csv_line,
    parse_symlinks_csv_line,
)
from ota_metadata.legacy.rs_table import ResourceTable


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
