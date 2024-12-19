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

import os
from hashlib import sha256
from pathlib import Path

import pytest

from ota_metadata.file_table._table import (
    FileTableDirectories,
    FileTableNonRegularFiles,
    FileTableRegularFiles,
)
from ota_metadata.file_table._types import FileEntryAttrs


def _check_same_stat(
    _fpath: Path,
    _entry: FileEntryAttrs,
    *,
    is_symlink: bool = False,
    check_size: bool = False,
):
    _stat_from_fpath = _fpath.stat()
    assert _stat_from_fpath.st_uid == _entry.uid
    assert _stat_from_fpath.st_gid == _entry.gid

    # shortpath the checks not suitable for symlink
    if is_symlink:
        return

    assert _stat_from_fpath.st_mode == _entry.mode
    if check_size:
        assert _stat_from_fpath.st_size == _entry.size


def _check_hardlink(_fpath: Path, _src: Path):
    assert _fpath.stat().st_ino == _src.stat().st_ino


def _check_xattr(_fpath: Path, _xattrs: dict[str, str]):
    for k, v in _xattrs.items():
        assert os.getxattr(_fpath, k) == v.encode()


@pytest.mark.parametrize(
    "_in",
    (
        (
            FileTableDirectories(
                path="/a/b/c/d/e",
                entry_attrs=FileEntryAttrs(
                    mode=0o040755,
                    uid=1000,
                    gid=1000,
                ),
            )
        ),
        (
            FileTableDirectories(
                path="/α/β/γ",
                entry_attrs=FileEntryAttrs(
                    mode=0o040755,
                    uid=1000,
                    gid=1000,
                    xattrs={
                        "user.Ελληνικό": "αλφάβητο",
                        "user.bar": "bar",
                    },
                ),
            )
        ),
    ),
)
def test_dir(_in: FileTableDirectories, tmp_path: Path):
    target = tmp_path
    _in.prepare_target(target_mnt=target)

    _fpath_on_target = tmp_path / Path(_in.path).relative_to("/")
    assert _fpath_on_target == _in.fpath_on_target(target_mnt=target)
    assert _check_same_stat(_fpath_on_target, _in.entry_attrs)
    if _xattrs := _in.entry_attrs.xattrs:
        assert _check_xattr(_fpath_on_target, _xattrs)


TEST_FILE_SIZE = 128  # bytes


@pytest.mark.parametrize(
    "file_contents, _in, prepare_method",
    (
        # normal file, no hardlink, no xattrs
        (
            file_contents := os.urandom(TEST_FILE_SIZE),
            FileTableRegularFiles(
                path="/a/b/c/d/e.bin",
                digest=sha256(file_contents).digest(),
                entry_attrs=FileEntryAttrs(
                    mode=0o100644,
                    uid=1000,
                    gid=1000,
                    size=TEST_FILE_SIZE,
                ),
            ),
            "copy",
        ),
        # normal file, hardlink, no xattrs
        (
            file_contents := os.urandom(TEST_FILE_SIZE),
            FileTableRegularFiles(
                path="/a/b/c/d/e.bin",
                digest=sha256(file_contents).digest(),
                entry_attrs=FileEntryAttrs(
                    mode=0o100644,
                    uid=1000,
                    gid=1000,
                    size=TEST_FILE_SIZE,
                    inode=67890,
                    xattrs={
                        "user.Ελληνικό": "αλφάβητο",
                        "user.bar": "bar",
                    },
                ),
            ),
            "hardlink",
        ),
        # normal file, no hardlink, xattrs
        (
            file_contents := os.urandom(TEST_FILE_SIZE),
            FileTableRegularFiles(
                path="/a/b/c/d/e.bin",
                digest=sha256(file_contents).digest(),
                entry_attrs=FileEntryAttrs(
                    mode=0o100644,
                    uid=1000,
                    gid=1000,
                    size=TEST_FILE_SIZE,
                    xattrs={
                        "user.Ελληνικό": "αλφάβητο",
                        "user.bar": "bar",
                    },
                ),
            ),
            "move",
        ),
        # normal file, hardlink, xattrs
        (
            file_contents := os.urandom(TEST_FILE_SIZE),
            FileTableRegularFiles(
                path="/α/β/γ/ζ.bin",
                digest=sha256(file_contents).digest(),
                entry_attrs=FileEntryAttrs(
                    mode=0o100644,
                    uid=1000,
                    gid=1000,
                    size=TEST_FILE_SIZE,
                    inode=67890,
                    xattrs={
                        "user.Ελληνικό": "αλφάβητο",
                        "user.bar": "bar",
                    },
                ),
            ),
            "hardlink",
        ),
    ),
)
def test_regular_file(
    file_contents: bytes,
    _in: FileTableRegularFiles,
    prepare_method,
    tmp_path: Path,
):
    src = tmp_path / "_test_file_contest"
    src.write_bytes(file_contents)
    _src_inode = src.stat().st_ino

    target = tmp_path
    _in.prepare_target(src, target_mnt=target, prepare_method=prepare_method)

    _fpath_on_target = tmp_path / Path(_in.path).relative_to("/")
    _check_same_stat(_fpath_on_target, _in.entry_attrs, check_size=True)

    if prepare_method == "hardlink":
        _check_hardlink(_fpath_on_target, src)
    elif prepare_method == "move":
        assert not src.exists() and _src_inode == _fpath_on_target.stat().st_ino

    if xattrs := _in.entry_attrs.xattrs:
        _check_xattr(_fpath_on_target, xattrs)


@pytest.mark.parametrize(
    "_in",
    (
        (
            FileTableNonRegularFiles(
                path="/a/b/c/d/symlink",
                contents="/α/β/γ/ζ.bin".encode(),
                entry_attrs=FileEntryAttrs(
                    mode=0o120777,
                    uid=1000,
                    gid=1000,
                ),
            )
        ),
    ),
)
def test_non_regular_file(_in: FileTableNonRegularFiles, tmp_path: Path):
    _in.prepare_target(target_mnt=tmp_path)
    _fpath_on_target = tmp_path / Path(_in.path).relative_to("/")

    assert _check_same_stat(_fpath_on_target, _in.entry_attrs, is_symlink=True)
    assert _in.contents and os.readlink(_fpath_on_target) == _in.contents.decode()
