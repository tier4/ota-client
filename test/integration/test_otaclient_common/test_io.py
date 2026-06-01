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
"""Integration tests for the otaclient_common._io atomic file primitives."""

from __future__ import annotations

import os
import random
import string
import subprocess
from hashlib import sha256
from pathlib import Path

import pytest

from otaclient_common._io import (
    TMP_FILE_PREFIX,
    _gen_tmp_fname,
    cal_file_digest,
    copyfile_atomic,
    file_sha256,
    remove_file,
    symlink_atomic,
    write_str_to_file_atomic,
)

# 3MiB each: (raw bytes, expected sha256 hexdigest)
CAL_FILE_DIGEST_TEST_DATA = [
    pytest.param(
        b"abcdefgh" * 393216,
        "bbeddf312779f8bb34907df04d2c5cc70bd68f62a06bc09864c50adff87354b2",
        id="ascii-pattern",
    ),
    pytest.param(
        0x1234567811223344.to_bytes(8, "big") * 393216,
        "8bb4daf6b0dd9aeb69da9f2b634951cb988413a81b72e14919329fa5951d3b0f",
        id="binary-pattern",
    ),
]


@pytest.mark.parametrize("contents, expected_digest", CAL_FILE_DIGEST_TEST_DATA)
def test_cal_file_digest(contents: bytes, expected_digest: str, tmp_path: Path):
    test_file = tmp_path / f"_test_file_digest_{os.urandom(6).hex()}"
    test_file.write_bytes(contents)

    assert cal_file_digest(test_file, algorithm="sha256") == expected_digest


class TestWriteStrToFileAtomic:
    # data payload sizes (bytes) exercised by each write scenario
    DATA_LENGTHS = [100, 500, 1000, 9000]

    @pytest.fixture(params=DATA_LENGTHS, ids=lambda _len: f"{_len}bytes")
    def payload(self, request: pytest.FixtureRequest) -> str:
        return "".join(random.choice(string.printable) for _ in range(request.param))

    def test_plain_write(self, payload: str, tmp_path: Path):
        _dst = tmp_path / _gen_tmp_fname("_test_file")
        write_str_to_file_atomic(_dst, payload, follow_symlink=False)

        # intentionally use an external subprocess call to ensure the changes
        #   are written to the disk properly.
        assert subprocess.check_output(["cat", str(_dst)]).decode() == payload

    def test_follow_symlink_writes_through_to_target(
        self, payload: str, tmp_path: Path
    ):
        _real_dst = tmp_path / _gen_tmp_fname("_test_file")
        _symlink = tmp_path / _gen_tmp_fname("_test_file_symlink")
        _symlink.symlink_to(_real_dst)

        write_str_to_file_atomic(_real_dst, payload, follow_symlink=True)

        assert subprocess.check_output(["cat", str(_symlink)]).decode() == payload

    def test_not_follow_symlink_preserves_symlink(self, payload: str, tmp_path: Path):
        _real_dst = tmp_path / _gen_tmp_fname("_test_file")
        _symlink = tmp_path / _gen_tmp_fname("_test_file_symlink")
        _symlink.symlink_to(_real_dst)

        write_str_to_file_atomic(_real_dst, payload, follow_symlink=False)

        assert subprocess.check_output(["cat", str(_symlink)]).decode() == payload
        assert _symlink.is_symlink() and _symlink.resolve() == _real_dst


@pytest.mark.parametrize(
    "preexisting_type, preexisting_contents, exc",
    (
        pytest.param("symlink", "/not/our/target", None, id="replace-existing-symlink"),
        pytest.param("notexisted", "", None, id="create-new-symlink"),
        pytest.param(
            "file", "somecontentssomecontents", None, id="replace-normal-file"
        ),
        pytest.param("symlink", "/a/target", None, id="already-correct-symlink"),
        pytest.param("dir", "", IsADirectoryError, id="src-is-dir-raises"),
    ),
)
def test_symlink_atomic(
    preexisting_type: str, preexisting_contents: str, exc, tmp_path: Path
):
    target = "/a/target"
    _workdir = tmp_path / "workdir"
    _workdir.mkdir()

    _symlink = _workdir / _gen_tmp_fname("_test_symlink")
    if preexisting_type == "symlink":
        _symlink.symlink_to(preexisting_contents)
    elif preexisting_type == "file":
        _symlink.write_text(preexisting_contents)
    elif preexisting_type == "dir":
        _symlink.mkdir()

    if exc:
        with pytest.raises(exc):
            symlink_atomic(_symlink, target)
    else:
        symlink_atomic(_symlink, target)
        assert str(_symlink.resolve()) == target  # ensure symlink is updated

    # ensure no tmp file leftover
    assert not list(_workdir.glob(f"{TMP_FILE_PREFIX}*"))


def test_copyfile_atomic(tmp_path: Path):
    _src = tmp_path / "_src"
    _dst = tmp_path / "_dst"

    _data = os.urandom(32) * 1024**2
    _data_hash = sha256(_data).hexdigest()

    with open(_src, "wb") as f:
        f.write(_data)
        f.flush()
        os.fsync(f.fileno())

    copyfile_atomic(_src, _dst)
    assert file_sha256(_dst) == file_sha256(_src) == _data_hash
    assert subprocess.check_output(["cat", str(_dst)]) == _data


def test_remove_file(tmp_path: Path):
    test_f = tmp_path / "test_f"
    symlink_target = tmp_path / "symlink_target_dir"
    symlink_target.mkdir()

    # NOTE: we DON'T want to accidentally remove the symlink target!
    test_f.symlink_to(symlink_target)
    assert test_f.is_symlink()
    remove_file(test_f)
    assert symlink_target.is_dir()
    assert not test_f.is_symlink() and not test_f.exists()

    test_f.touch()
    assert test_f.is_file()
    remove_file(test_f)
    assert not test_f.is_file() and not test_f.exists()

    test_f.mkdir()
    assert test_f.is_dir()
    remove_file(test_f)
    assert not test_f.is_dir() and not test_f.exists()
