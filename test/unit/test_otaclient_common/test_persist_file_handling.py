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

from typing import Mapping
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from otaclient_common.linux import ParsedGroup, ParsedPasswd
from otaclient_common.persist_file_handling import PersistFilesHandler

MODULE = "otaclient_common.persist_file_handling."


#
# ------------ Helpers ------------ #
#
# Build ParsedPasswd / ParsedGroup directly from in-memory dicts (bypass file
# reading; ParsedPasswd.__init__ would otherwise open a file).
#
def _make_passwd(by_name: Mapping[str, int]) -> ParsedPasswd:
    pp = ParsedPasswd.__new__(ParsedPasswd)
    pp._by_name = dict(by_name)
    pp._by_uid = {v: k for k, v in pp._by_name.items()}
    return pp


def _make_group(by_name: Mapping[str, int]) -> ParsedGroup:
    pg = ParsedGroup.__new__(ParsedGroup)
    pg._by_name = dict(by_name)
    pg._by_gid = {v: k for k, v in pg._by_name.items()}
    return pg


def _make_handler(
    mocker: MockerFixture,
    *,
    src_passwd: Mapping[str, int],
    dst_passwd: Mapping[str, int],
    src_group: Mapping[str, int],
    dst_group: Mapping[str, int],
) -> PersistFilesHandler:
    """Construct PersistFilesHandler with patched ParsedPasswd/ParsedGroup.

    The patched classes return pre-built in-memory instances regardless of the
    file path argument, so no real passwd/group file is needed.
    """
    mocker.patch(
        MODULE + "ParsedPasswd",
        side_effect=[_make_passwd(src_passwd), _make_passwd(dst_passwd)],
    )
    mocker.patch(
        MODULE + "ParsedGroup",
        side_effect=[_make_group(src_group), _make_group(dst_group)],
    )
    return PersistFilesHandler(
        src_passwd_file="<unused>",
        src_group_file="<unused>",
        dst_passwd_file="<unused>",
        dst_group_file="<unused>",
        src_root="/src",
        dst_root="/dst",
    )


#
# ------------ uid/gid mapping ------------ #
#
class TestChownWithMapping:
    """_chown_with_mapping translates uid/gid via passwd/group databases.

    Mapping rule (per the class docstring):
        src_uid -> src_name -> dst_name -> dst_uid
        src_gid -> src_name -> dst_name -> dst_gid

    On a mapping failure the original src uid/gid is kept unchanged.
    """

    @pytest.fixture(autouse=True)
    def setup(self, mocker: MockerFixture):
        # alice/bob exist on both sides with renumbered ids; carol exists only
        # on src (uid will be unmappable); group "wheel" only on src.
        self.handler = _make_handler(
            mocker,
            src_passwd={"alice": 1000, "bob": 1001, "carol": 1002},
            dst_passwd={"alice": 2000, "bob": 2001},
            src_group={"alice": 1000, "bob": 1001, "wheel": 1010},
            dst_group={"alice": 3000, "bob": 3001},
        )
        self.chown = mocker.patch(MODULE + "os.chown")

    def _stat(self, uid: int, gid: int) -> MagicMock:
        st = MagicMock(spec=["st_uid", "st_gid"])
        st.st_uid = uid
        st.st_gid = gid
        return st

    def test_uses_mapped_uid_and_gid(self):
        """Both ids resolve through src_name -> dst_name."""
        self.handler._chown_with_mapping(self._stat(1000, 1001), "/dst/path")
        self.chown.assert_called_once_with(
            "/dst/path", uid=2000, gid=3001, follow_symlinks=False
        )

    def test_keeps_unmappable_uid(self):
        """uid present in src but not in dst -> keep src uid unchanged."""
        # carol exists only on src side.
        self.handler._chown_with_mapping(self._stat(1002, 1000), "/dst/path")
        self.chown.assert_called_once_with(
            "/dst/path", uid=1002, gid=3000, follow_symlinks=False
        )

    def test_keeps_unmappable_gid(self):
        """gid present in src but not in dst -> keep src gid unchanged."""
        # wheel exists only on src side.
        self.handler._chown_with_mapping(self._stat(1000, 1010), "/dst/path")
        self.chown.assert_called_once_with(
            "/dst/path", uid=2000, gid=1010, follow_symlinks=False
        )

    def test_keeps_uid_unknown_in_src(self):
        """uid not in src passwd at all -> mapping raises -> keep src uid."""
        self.handler._chown_with_mapping(self._stat(99999, 1000), "/dst/path")
        self.chown.assert_called_once_with(
            "/dst/path", uid=99999, gid=3000, follow_symlinks=False
        )

    def test_keeps_both_when_neither_mappable(self):
        self.handler._chown_with_mapping(self._stat(99999, 99998), "/dst/path")
        self.chown.assert_called_once_with(
            "/dst/path", uid=99999, gid=99998, follow_symlinks=False
        )


class TestMapperMemoization:
    """The uid/gid mapper functions are wrapped with lru_cache.

    Repeated calls with the same id should not re-run the underlying mapping.
    """

    @pytest.fixture(autouse=True)
    def setup(self, mocker: MockerFixture):
        self.handler = _make_handler(
            mocker,
            src_passwd={"alice": 1000, "bob": 1001},
            dst_passwd={"alice": 2000, "bob": 2001},
            src_group={"alice": 1000},
            dst_group={"alice": 3000},
        )

    def test_uid_mapper_caches_result(self):
        for _ in range(5):
            assert self.handler._uid_mapper(uid=1000) == 2000
        info = self.handler._uid_mapper.cache_info()
        assert info.misses == 1
        assert info.hits == 4

    def test_gid_mapper_caches_result(self):
        for _ in range(3):
            assert self.handler._gid_mapper(gid=1000) == 3000
        info = self.handler._gid_mapper.cache_info()
        assert info.misses == 1
        assert info.hits == 2

    def test_distinct_ids_each_count_as_miss(self):
        self.handler._uid_mapper(uid=1000)
        self.handler._uid_mapper(uid=1001)
        self.handler._uid_mapper(uid=1000)
        info = self.handler._uid_mapper.cache_info()
        assert info.misses == 2
        assert info.hits == 1


#
# ------------ _rm_target classifier ------------ #
#
class TestRmTarget:
    """_rm_target dispatches on the *type* of the destination entry.

    The classification order matters: `is_file_or_symlink` is checked before
    `is_dir`, because `is_dir()` follows symlinks, so a symlink-to-dir would be
    misclassified if checked dir-first.
    """

    def _target(self, mocker: MockerFixture) -> MagicMock:
        # spec=["unlink", "is_dir", "exists"] mirrors the methods _rm_target
        # actually calls on the target.
        return mocker.MagicMock(spec=["unlink", "is_dir", "exists"])

    def test_unlinks_when_file_or_symlink(self, mocker: MockerFixture):
        target = self._target(mocker)
        mocker.patch(MODULE + "is_file_or_symlink", return_value=True)
        rmtree = mocker.patch(MODULE + "shutil.rmtree")

        PersistFilesHandler._rm_target(target)

        target.unlink.assert_called_once_with(missing_ok=True)
        rmtree.assert_not_called()
        target.is_dir.assert_not_called()

    def test_rmtree_when_directory(self, mocker: MockerFixture):
        target = self._target(mocker)
        mocker.patch(MODULE + "is_file_or_symlink", return_value=False)
        target.is_dir.return_value = True
        rmtree = mocker.patch(MODULE + "shutil.rmtree")

        PersistFilesHandler._rm_target(target)

        rmtree.assert_called_once_with(target, ignore_errors=True)
        target.unlink.assert_not_called()

    def test_noop_when_missing(self, mocker: MockerFixture):
        target = self._target(mocker)
        mocker.patch(MODULE + "is_file_or_symlink", return_value=False)
        target.is_dir.return_value = False
        target.exists.return_value = False
        rmtree = mocker.patch(MODULE + "shutil.rmtree")

        # Must not raise; must not call unlink/rmtree.
        PersistFilesHandler._rm_target(target)

        target.unlink.assert_not_called()
        rmtree.assert_not_called()

    def test_raises_on_special_file(self, mocker: MockerFixture):
        """Existing non-file/symlink/dir target (FIFO, socket, device)."""
        target = self._target(mocker)
        mocker.patch(MODULE + "is_file_or_symlink", return_value=False)
        target.is_dir.return_value = False
        target.exists.return_value = True

        with pytest.raises(ValueError, match="not normal file/symlink/dir"):
            PersistFilesHandler._rm_target(target)


#
# ------------ preserve_persist_entry input validation ------------ #
#
class TestPreservePersistEntryInputValidation:
    """preserve_persist_entry rejects non-canonical persist entries.

    persist entries listed in persists.txt must be absolute paths rooted at
    `/` so that `Path(entry).relative_to("/")` yields a valid relative path.
    Anything else raises before any filesystem I/O happens.
    """

    @pytest.fixture(autouse=True)
    def setup(self, mocker: MockerFixture):
        self.handler = _make_handler(
            mocker,
            src_passwd={"root": 0},
            dst_passwd={"root": 0},
            src_group={"root": 0},
            dst_group={"root": 0},
        )

    @pytest.mark.parametrize(
        "bad_entry",
        [
            pytest.param("relative/path", id="relative_path"),
            pytest.param("./relative", id="dot_relative"),
            pytest.param("../escape", id="parent_relative"),
        ],
    )
    def test_non_absolute_entry_raises(self, bad_entry: str):
        with pytest.raises(ValueError):
            self.handler.preserve_persist_entry(bad_entry)
