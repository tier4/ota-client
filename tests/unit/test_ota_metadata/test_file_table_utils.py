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
"""Unit tests for the regular file preparation helpers of the file_table.

Scope: the mode a regular file gets on the standby slot. The tests run as an
unprivileged user, so a root-owned entry takes the no-chown path, which is
the one the umask affected.
"""

from __future__ import annotations

import os
import stat

import pytest
from ota_image_libs.v1.file_table.db import RegularFileRow

from ota_metadata.file_table.utils import (
    prepare_regular_copy,
    prepare_regular_inlined,
)

ROOT_OWNED_MODES = [0o100664, 0o100666, 0o104755]


@pytest.fixture
def umask_022():
    """Run the test under the common umask 022, then restore the original."""
    _old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(_old)


@pytest.fixture
def target_mnt(tmp_path):
    _mnt = tmp_path / "mnt"
    (_mnt / "test").mkdir(parents=True)
    return _mnt


@pytest.mark.parametrize("mode", ROOT_OWNED_MODES)
def test_prepare_regular_copy_root_owned_mode(tmp_path, target_mnt, umask_022, mode):
    source_file = tmp_path / "source.txt"
    source_file.write_bytes(b"content")
    entry = RegularFileRow(
        path="/test/file.txt",
        uid=0,
        gid=0,
        mode=mode,
        digest=b"digest",
        size=7,
        inode_id=1,
    )

    result = prepare_regular_copy(entry, source_file, target_mnt=target_mnt)

    assert result.read_bytes() == b"content"
    assert stat.S_IMODE(result.stat().st_mode) == stat.S_IMODE(mode)


def test_prepare_regular_copy_root_owned_existing_file(tmp_path, target_mnt, umask_022):
    (target_mnt / "test" / "file.txt").touch(mode=0o600)
    source_file = tmp_path / "source.txt"
    source_file.write_bytes(b"content")
    entry = RegularFileRow(
        path="/test/file.txt",
        uid=0,
        gid=0,
        mode=0o100644,
        digest=b"digest",
        size=7,
        inode_id=1,
    )

    result = prepare_regular_copy(entry, source_file, target_mnt=target_mnt)

    assert stat.S_IMODE(result.stat().st_mode) == 0o644


@pytest.mark.parametrize("contents", [b"data", None])
@pytest.mark.parametrize("mode", ROOT_OWNED_MODES)
def test_prepare_regular_inlined_root_owned_mode(target_mnt, umask_022, mode, contents):
    entry = RegularFileRow(
        path="/test/file.txt",
        uid=0,
        gid=0,
        mode=mode,
        digest=b"digest",
        size=len(contents) if contents else 0,
        inode_id=1,
        contents=contents,
    )

    result = prepare_regular_inlined(entry, target_mnt=target_mnt)

    assert result.read_bytes() == (contents or b"")
    assert stat.S_IMODE(result.stat().st_mode) == stat.S_IMODE(mode)
