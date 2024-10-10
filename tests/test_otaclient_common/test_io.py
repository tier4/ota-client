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
    symlink_atomic,
    write_str_to_file_atomic,
)

# 3MiB each
CAL_FILE_DIGEST_TEST_DATA = [
    (
        b"abcdefgh" * 393216,
        "bbeddf312779f8bb34907df04d2c5cc70bd68f62a06bc09864c50adff87354b2",
    ),
    (
        0x1234567811223344.to_bytes(8, "big") * 393216,
        "8bb4daf6b0dd9aeb69da9f2b634951cb988413a81b72e14919329fa5951d3b0f",
    ),
]


def test_gen_file_digest(tmp_path: Path):
    for _test_tup in CAL_FILE_DIGEST_TEST_DATA:
        _tmp_file = tmp_path / f"_test_file_digest_tmp_{os.urandom(6).hex()}"

        contents, sha256_hash = _test_tup
        try:
            _tmp_file.write_bytes(contents)
            assert cal_file_digest(_tmp_file, algorithm="sha256") == sha256_hash
        finally:
            _tmp_file.unlink(missing_ok=True)


class TestWriteStrToFileAtomic:

    @pytest.fixture(scope="class", autouse=True)
    def setup_test(self):
        self.data_lens = [100, 500, 1000, 9000]  # bytes
        self.data = [
            "".join([random.choice(string.printable) for _ in range(_len)])
            for _len in self.data_lens
        ]

    def test_write_str_to_file_atomic(self, tmp_path: Path):
        for data in self.data:
            _tmp_dst = tmp_path / _gen_tmp_fname("_test_file")
            write_str_to_file_atomic(_tmp_dst, data, follow_symlink=False)

            # intensionally use external subprocess call to ensure the changes
            #   are written to the disk properly.
            assert subprocess.check_output(["cat", str(_tmp_dst)]) == data

    def test_write_str_to_file_atomic_follow_symlink(self, tmp_path: Path):
        for data in self.data:
            _tmp_real_dst = tmp_path / _gen_tmp_fname("_test_file")
            _tmp_dst_symlink = tmp_path / _gen_tmp_fname("_test_file_symlink")
            _tmp_dst_symlink.symlink_to(_tmp_real_dst)

            write_str_to_file_atomic(_tmp_real_dst, data, follow_symlink=True)
            assert subprocess.check_output(["cat", str(_tmp_dst_symlink)]) == data

    def test_write_str_to_file_atomic_not_follow_symlink(self, tmp_path: Path):
        for data in self.data:
            _tmp_real_dst = tmp_path / _gen_tmp_fname("_test_file")
            _tmp_dst_symlink = tmp_path / _gen_tmp_fname("_test_file_symlink")
            _tmp_dst_symlink.symlink_to(_tmp_real_dst)

            write_str_to_file_atomic(_tmp_real_dst, data, follow_symlink=False)
            assert subprocess.check_output(["cat", str(_tmp_dst_symlink)]) == data
            assert (
                _tmp_dst_symlink.is_symlink()
                and _tmp_dst_symlink.resolve() == _tmp_real_dst
            )


@pytest.mark.parametrize(
    "target, symlink_contents, exc",
    (
        # symlink already existed
        ("/a/target", "symlink=/not/our/target", None),
        # create new symlink
        ("/a/target", "notexisted=", None),
        # src is a normal file
        ("/a/target", "file=somecontentssomecontents", None),
        # src is already the correct symlink
        ("/a/target", "symlink=/a/target", None),
        # src is a directory
        ("/a/target", "dir=", IsADirectoryError),
    ),
)
def test_symlink_atomic(target: str, symlink_contents: str, exc, tmp_path: Path):
    _workdir = tmp_path / "workdir"
    _workdir.mkdir()

    _symlink = _workdir / _gen_tmp_fname("_test_symlink")
    _type, _contents = symlink_contents.split("=")
    if _type == "symlink":
        _symlink.symlink_to(_contents)
    elif _type == "file":
        _symlink.write_text(_contents)
    elif _type == "dir":
        _symlink.mkdir()

    if exc:
        with pytest.raises(exc):
            symlink_atomic(_symlink, target)

    else:
        symlink_atomic(_symlink, target)
        assert _symlink.resolve() == target  # ensure symlink is updated

    assert not list(_workdir.glob(f"{TMP_FILE_PREFIX}*"))  # ensure no tmp file leftover


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
