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

from ota_metadata.file_table.utils import (
    DirTypedDict,
    NonRegularFileTypedDict,
    RegularFileTypedDict,
    fpath_on_target,
    prepare_dir,
    prepare_non_regular,
    prepare_regular,
)
from otaclient_common._typing import StrOrPath
from tests.utils import check_same_stat


def _check_hardlink(_fpath: Path, _src: Path):
    assert _fpath.stat().st_ino == _src.stat().st_ino


@pytest.mark.parametrize(
    "_canon_path, target_mnt, expected",
    (
        (
            "/",
            Path("/run/otaclient/mnt/standby_slot"),
            Path("/run/otaclient/mnt/standby_slot"),
        ),
        (
            Path("/abc"),
            "/run/otaclient/mnt/standby_slot",
            Path("/run/otaclient/mnt/standby_slot/abc"),
        ),
        (
            Path("/a/b/c"),
            Path("/run/otaclient/mnt/standby_slot"),
            Path("/run/otaclient/mnt/standby_slot/a/b/c"),
        ),
    ),
)
def test_fpath_on_target(
    _canon_path: StrOrPath, target_mnt: StrOrPath, expected: Path
) -> None:
    assert fpath_on_target(_canon_path, target_mnt) == expected


@pytest.mark.parametrize(
    "_in",
    (
        DirTypedDict(
            path="/a/b/c/d/e",
            mode=0o040755,
            uid=5123,
            gid=2345,
        ),
        DirTypedDict(
            path="/α/β/γ",
            mode=0o040755,
            uid=1000,
            gid=1000,
        ),
    ),
)
def test_prepare_dir(_in: DirTypedDict, tmp_path: Path):
    target = tmp_path
    prepare_dir(_in, target_mnt=target)

    _fpath_on_target = tmp_path / Path(_in["path"]).relative_to("/")
    assert _fpath_on_target == fpath_on_target(_in["path"], target_mnt=target)
    check_same_stat(_fpath_on_target, uid=_in["uid"], gid=_in["gid"], mode=_in["mode"])


@pytest.mark.parametrize(
    "_in",
    (
        (
            NonRegularFileTypedDict(
                path="/a/b/c/d/symlink",
                meta="/α/β/γ/ζ.bin".encode(),
                links_count=None,
                mode=0o120777,
                uid=1000,
                gid=1000,
                xattrs=None,
            )
        ),
    ),
)
def test_prepare_symlink(_in: NonRegularFileTypedDict, tmp_path: Path):
    _fpath_on_target = tmp_path / Path(_in["path"]).relative_to("/")
    # NOTE: symlink preapre_target doesn't prepare parent, we need to do it by ourselves.
    _fpath_on_target.parent.mkdir(exist_ok=True, parents=True)

    prepare_non_regular(_in, target_mnt=tmp_path)

    check_same_stat(
        _fpath_on_target,
        uid=_in["uid"],
        gid=_in["gid"],
        mode=_in["mode"],
        is_symlink=True,
    )
    assert _in["meta"] and os.readlink(_fpath_on_target) == _in["meta"].decode()


TEST_FILE_SIZE = 64  # bytes


@pytest.mark.parametrize(
    "file_contents, _in, prepare_method",
    (
        # normal file, no hardlink, no xattrs
        (
            file_contents := os.urandom(TEST_FILE_SIZE),
            RegularFileTypedDict(
                path="/a/b/c/d/e.bin",
                digest=sha256(file_contents).digest(),
                mode=0o100644,
                uid=1000,
                gid=1000,
                size=TEST_FILE_SIZE,
                # following fields are not used.
                # NOTE: xattrs is not supported by old OTA image, so
                #       just ignored it for now.
                links_count=None,
                xattrs=None,
                inode_id=123,
            ),
            "copy",
        ),
        # normal file, hardlink, no xattrs
        (
            file_contents := os.urandom(TEST_FILE_SIZE),
            RegularFileTypedDict(
                path="/a/b/c/d/e.bin",
                digest=sha256(file_contents).digest(),
                mode=0o100644,
                uid=1000,
                gid=1000,
                size=TEST_FILE_SIZE,
                # following fields are not used.
                # NOTE: xattrs is not supported by old OTA image, so
                #       just ignored it for now.
                links_count=None,
                xattrs=None,
                inode_id=123,
            ),
            "hardlink",
        ),
        # normal file, no hardlink, xattrs
        (
            file_contents := os.urandom(TEST_FILE_SIZE),
            RegularFileTypedDict(
                path="/a/b/c/d/e.bin",
                digest=sha256(file_contents).digest(),
                mode=0o100644,
                uid=1000,
                gid=1000,
                size=TEST_FILE_SIZE,
                # following fields are not used.
                # NOTE: xattrs is not supported by old OTA image, so
                #       just ignored it for now.
                links_count=None,
                xattrs=None,
                inode_id=123,
            ),
            "move",
        ),
        # normal file, hardlink, xattrs
        (
            file_contents := os.urandom(TEST_FILE_SIZE),
            RegularFileTypedDict(
                path="/α/β/γ/ζ.bin",
                digest=sha256(file_contents).digest(),
                mode=0o100644,
                uid=1000,
                gid=1000,
                size=TEST_FILE_SIZE,
                # following fields are not used.
                # NOTE: xattrs is not supported by old OTA image, so
                #       just ignored it for now.
                links_count=None,
                xattrs=None,
                inode_id=123,
            ),
            "hardlink",
        ),
    ),
)
def test_preapre_regular_file(
    file_contents: bytes,
    _in: RegularFileTypedDict,
    prepare_method,
    tmp_path: Path,
):
    src = tmp_path / "_test_file_contest"
    src.write_bytes(file_contents)
    _src_inode = src.stat().st_ino

    target = tmp_path
    _fpath_on_target = tmp_path / Path(_in["path"]).relative_to("/")
    # NOTE that prepare_target doesn't prepare the parents of the entry,
    #   we need to do it by ourselves.
    _fpath_on_target.parent.mkdir(exist_ok=True, parents=True)

    prepare_regular(_in, src, target_mnt=target, prepare_method=prepare_method)

    check_same_stat(
        _fpath_on_target,
        uid=_in["uid"],
        gid=_in["gid"],
        mode=_in["mode"],
        size=TEST_FILE_SIZE,
        check_size=True,
    )

    if prepare_method == "hardlink":
        _check_hardlink(_fpath_on_target, src)
    elif prepare_method == "move":
        assert not src.exists() and _src_inode == _fpath_on_target.stat().st_ino
