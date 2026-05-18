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
import stat
from pathlib import Path

import pytest

from otaclient_common.persist_file_handling import PersistFilesHandler

from .conftest import (
    UNMAPPABLE_GID,
    UNMAPPABLE_UID,
    Roots,
    assert_uid_gid_mode,
    dst_gid,
    dst_uid,
    make_owned_dir,
    make_owned_file,
    make_owned_symlink,
    persist_entry_for,
    src_gid,
    src_uid,
    stat_uid_gid_mode,
)

# Linux always reports symlink mode bits as 0o777 via lstat.
SYMLINK_MODE = 0o777


#
# ------------ Preserving a regular file ------------ #
#
class TestPreserveFile:
    """preserve_persist_entry for a regular file at the entry path."""

    def test_creates_dst_file_with_content_and_mapped_attrs(
        self, roots: Roots, handler: PersistFilesHandler
    ):
        src_file = make_owned_file(
            roots.src / "f.txt",
            "hello",
            uid=src_uid("alice"),
            gid=src_gid("agroup"),
            mode=0o644,
        )

        handler.preserve_persist_entry(persist_entry_for(src_file, roots.src))

        dst_file = roots.dst / "f.txt"
        assert dst_file.is_file() and not dst_file.is_symlink()
        assert dst_file.read_text() == "hello"
        assert_uid_gid_mode(
            dst_file,
            uid=dst_uid("alice"),
            gid=dst_gid("agroup"),
            mode=0o644,
        )

    def test_creates_missing_parent_dirs_from_src(
        self, roots: Roots, handler: PersistFilesHandler
    ):
        # src/A is a dir owned by alice/agroup; src/A/f.txt is a file owned
        # by bob/bgroup. dst is empty -> _prepare_parent must create dst/A
        # mirroring src/A.
        make_owned_dir(
            roots.src / "A",
            uid=src_uid("alice"),
            gid=src_gid("agroup"),
            mode=0o750,
        )
        src_file = make_owned_file(
            roots.src / "A" / "f.txt",
            "x",
            uid=src_uid("bob"),
            gid=src_gid("bgroup"),
            mode=0o600,
        )

        handler.preserve_persist_entry(persist_entry_for(src_file, roots.src))

        # dst/A inherits src/A's perms via _prepare_dir
        assert_uid_gid_mode(
            roots.dst / "A",
            uid=dst_uid("alice"),
            gid=dst_gid("agroup"),
            mode=0o750,
        )
        # dst/A/f.txt is the preserved file
        assert_uid_gid_mode(
            roots.dst / "A" / "f.txt",
            uid=dst_uid("bob"),
            gid=dst_gid("bgroup"),
            mode=0o600,
        )

    def test_overwrites_existing_dst_file(
        self, roots: Roots, handler: PersistFilesHandler
    ):
        src_file = make_owned_file(
            roots.src / "f.txt",
            "new",
            uid=src_uid("alice"),
            gid=src_gid("agroup"),
            mode=0o644,
        )
        # Pre-existing dst file with different content/perms.
        make_owned_file(
            roots.dst / "f.txt",
            "stale",
            uid=src_uid("bob"),
            gid=src_gid("bgroup"),
            mode=0o600,
        )

        handler.preserve_persist_entry(persist_entry_for(src_file, roots.src))

        dst_file = roots.dst / "f.txt"
        assert dst_file.read_text() == "new"
        assert_uid_gid_mode(
            dst_file,
            uid=dst_uid("alice"),
            gid=dst_gid("agroup"),
            mode=0o644,
        )

    def test_replaces_dst_dir_at_entry_with_file(
        self, roots: Roots, handler: PersistFilesHandler
    ):
        """If dst path exists as a directory, _rm_target rmtrees it first."""
        src_file = make_owned_file(
            roots.src / "f.txt",
            "content",
            uid=src_uid("alice"),
            gid=src_gid("agroup"),
            mode=0o644,
        )
        # Pre-existing dst dir at the entry path (with a child to confirm
        # rmtree, not just unlink).
        dst_dir = roots.dst / "f.txt"
        dst_dir.mkdir()
        (dst_dir / "stale_child").write_text("garbage")

        handler.preserve_persist_entry(persist_entry_for(src_file, roots.src))

        dst_file = roots.dst / "f.txt"
        assert dst_file.is_file() and not dst_file.is_symlink()
        assert dst_file.read_text() == "content"


#
# ------------ Preserving a symlink ------------ #
#
class TestPreserveSymlink:
    """preserve_persist_entry for a symlink at the entry path.

    Symlink mode bits are always 0o777 on Linux (the kernel does not honor
    chmod on a symlink), so the handler does not chmod symlinks — it only
    preserves the link target and chowns via lchown.
    """

    @pytest.mark.parametrize(
        "link_target, link_kind",
        [
            pytest.param("real_target", "regular", id="regular_symlink"),
            pytest.param("missing_target", "broken", id="broken_symlink"),
        ],
    )
    def test_preserves_symlink_target_and_maps_attrs(
        self,
        roots: Roots,
        handler: PersistFilesHandler,
        link_target: str,
        link_kind: str,
    ):
        if link_kind == "regular":
            make_owned_file(
                roots.src / link_target,
                "data",
                uid=src_uid("alice"),
                gid=src_gid("agroup"),
                mode=0o644,
            )
        src_link = make_owned_symlink(
            roots.src / "lnk",
            link_target,
            uid=src_uid("bob"),
            gid=src_gid("bgroup"),
        )

        handler.preserve_persist_entry(persist_entry_for(src_link, roots.src))

        dst_link = roots.dst / "lnk"
        assert dst_link.is_symlink()
        assert os.readlink(dst_link) == link_target
        assert_uid_gid_mode(
            dst_link,
            uid=dst_uid("bob"),
            gid=dst_gid("bgroup"),
            mode=SYMLINK_MODE,
        )

    def test_keeps_unmappable_uid_gid_unchanged(
        self, roots: Roots, handler: PersistFilesHandler
    ):
        """Owner ids absent from passwd/group must be kept on dst as-is."""
        src_link = make_owned_symlink(
            roots.src / "lnk",
            "wherever",
            uid=UNMAPPABLE_UID,
            gid=UNMAPPABLE_GID,
        )

        handler.preserve_persist_entry(persist_entry_for(src_link, roots.src))

        dst_link = roots.dst / "lnk"
        assert dst_link.is_symlink()
        assert_uid_gid_mode(
            dst_link, uid=UNMAPPABLE_UID, gid=UNMAPPABLE_GID, mode=SYMLINK_MODE
        )

    def test_overwrites_existing_dst_symlink(
        self, roots: Roots, handler: PersistFilesHandler
    ):
        src_link = make_owned_symlink(
            roots.src / "lnk",
            "new_target",
            uid=src_uid("alice"),
            gid=src_gid("agroup"),
        )
        # Pre-existing dst symlink pointing somewhere else.
        (roots.dst / "lnk").symlink_to("old_target")

        handler.preserve_persist_entry(persist_entry_for(src_link, roots.src))

        assert os.readlink(roots.dst / "lnk") == "new_target"

    def test_replaces_dst_file_with_symlink(
        self, roots: Roots, handler: PersistFilesHandler
    ):
        src_link = make_owned_symlink(
            roots.src / "lnk",
            "target",
            uid=src_uid("alice"),
            gid=src_gid("agroup"),
        )
        # Pre-existing regular file at dst entry.
        (roots.dst / "lnk").write_text("not-a-symlink")

        handler.preserve_persist_entry(persist_entry_for(src_link, roots.src))

        dst_link = roots.dst / "lnk"
        assert dst_link.is_symlink()
        assert os.readlink(dst_link) == "target"


#
# ------------ Preserving a directory recursively ------------ #
#
class TestPreserveDirectory:
    """preserve_persist_entry for a directory at the entry path.

    The walk is followlinks=False: a symlinked subdirectory in the src tree
    is preserved as a symlink at dst, not recursed into.
    """

    @pytest.fixture
    def populated_src_dir(self, roots: Roots) -> Path:
        """Build src/A with a representative mix of children.

        Layout:
          src/A/                  dir alice/agroup mode 0o755
          src/A/file.txt          file bob/bgroup mode 0o640
          src/A/lnk_to_file       -> file.txt, owner carol/cgroup
          src/A/lnk_broken        -> nowhere, owner dave/dgroup
          src/A/lnk_unmappable    -> file.txt, owner UNMAPPABLE_UID/GID
          src/A/sub/              dir eve/egroup mode 0o700
          src/A/sub/inner.txt     file frank/fgroup mode 0o600
        """
        a = make_owned_dir(
            roots.src / "A",
            uid=src_uid("alice"),
            gid=src_gid("agroup"),
            mode=0o755,
        )
        make_owned_file(
            a / "file.txt",
            "FILE",
            uid=src_uid("bob"),
            gid=src_gid("bgroup"),
            mode=0o640,
        )
        make_owned_symlink(
            a / "lnk_to_file",
            "file.txt",
            uid=src_uid("carol"),
            gid=src_gid("cgroup"),
        )
        make_owned_symlink(
            a / "lnk_broken",
            "nowhere",
            uid=src_uid("dave"),
            gid=src_gid("dgroup"),
        )
        make_owned_symlink(
            a / "lnk_unmappable",
            "file.txt",
            uid=UNMAPPABLE_UID,
            gid=UNMAPPABLE_GID,
        )
        sub = make_owned_dir(
            a / "sub",
            uid=src_uid("eve"),
            gid=src_gid("egroup"),
            mode=0o700,
        )
        make_owned_file(
            sub / "inner.txt",
            "INNER",
            uid=src_uid("frank"),
            gid=src_gid("fgroup"),
            mode=0o600,
        )
        return a

    def test_copies_full_subtree_with_mapped_attrs(
        self,
        roots: Roots,
        handler: PersistFilesHandler,
        populated_src_dir: Path,
    ):
        handler.preserve_persist_entry(persist_entry_for(populated_src_dir, roots.src))

        dst_a = roots.dst / "A"
        # dir itself
        assert dst_a.is_dir() and not dst_a.is_symlink()
        assert_uid_gid_mode(
            dst_a, uid=dst_uid("alice"), gid=dst_gid("agroup"), mode=0o755
        )
        # regular file
        assert (dst_a / "file.txt").read_text() == "FILE"
        assert_uid_gid_mode(
            dst_a / "file.txt",
            uid=dst_uid("bob"),
            gid=dst_gid("bgroup"),
            mode=0o640,
        )
        # regular symlink
        assert (dst_a / "lnk_to_file").is_symlink()
        assert os.readlink(dst_a / "lnk_to_file") == "file.txt"
        assert_uid_gid_mode(
            dst_a / "lnk_to_file",
            uid=dst_uid("carol"),
            gid=dst_gid("cgroup"),
            mode=SYMLINK_MODE,
        )
        # broken symlink: link is preserved, target is not resolved
        assert (dst_a / "lnk_broken").is_symlink()
        assert not (dst_a / "lnk_broken").exists()
        assert os.readlink(dst_a / "lnk_broken") == "nowhere"
        assert_uid_gid_mode(
            dst_a / "lnk_broken",
            uid=dst_uid("dave"),
            gid=dst_gid("dgroup"),
            mode=SYMLINK_MODE,
        )
        # symlink with unmappable owner ids
        assert_uid_gid_mode(
            dst_a / "lnk_unmappable",
            uid=UNMAPPABLE_UID,
            gid=UNMAPPABLE_GID,
            mode=SYMLINK_MODE,
        )
        # nested dir + file
        assert_uid_gid_mode(
            dst_a / "sub",
            uid=dst_uid("eve"),
            gid=dst_gid("egroup"),
            mode=0o700,
        )
        assert (dst_a / "sub" / "inner.txt").read_text() == "INNER"
        assert_uid_gid_mode(
            dst_a / "sub" / "inner.txt",
            uid=dst_uid("frank"),
            gid=dst_gid("fgroup"),
            mode=0o600,
        )

    def test_skips_special_files(self, roots: Roots, handler: PersistFilesHandler):
        """FIFOs / sockets / devices in src are silently skipped (logged)."""
        a = make_owned_dir(
            roots.src / "A",
            uid=src_uid("alice"),
            gid=src_gid("agroup"),
            mode=0o755,
        )
        os.mkfifo(a / "pipe")
        # Confirm the fixture itself produced a FIFO.
        assert stat.S_ISFIFO(os.stat(a / "pipe").st_mode)

        handler.preserve_persist_entry(persist_entry_for(a, roots.src))

        assert (roots.dst / "A").is_dir()
        # pipe was not preserved at dst, but the rest of the dir was.
        assert not (roots.dst / "A" / "pipe").exists()
        assert not (roots.dst / "A" / "pipe").is_symlink()

    def test_replaces_existing_dst_subdir_contents(
        self, roots: Roots, handler: PersistFilesHandler
    ):
        """Pre-existing dst entries inside the preserved dir are wiped.

        _recursively_prepare_dir calls _rm_target on dst_cur_dpath before
        re-preparing it, so dst/A is rmtree'd before being recreated.
        """
        # src/A with a single file.
        a = make_owned_dir(
            roots.src / "A",
            uid=src_uid("alice"),
            gid=src_gid("agroup"),
            mode=0o755,
        )
        make_owned_file(
            a / "keep.txt",
            "kept",
            uid=src_uid("bob"),
            gid=src_gid("bgroup"),
            mode=0o644,
        )
        # Pre-populate dst/A with stale entries that must NOT survive.
        dst_a = roots.dst / "A"
        dst_a.mkdir()
        (dst_a / "stale_file").write_text("stale")
        (dst_a / "stale_sub").mkdir()
        (dst_a / "stale_sub" / "deeper").write_text("stale too")

        handler.preserve_persist_entry(persist_entry_for(a, roots.src))

        assert (dst_a / "keep.txt").read_text() == "kept"
        assert not (dst_a / "stale_file").exists()
        assert not (dst_a / "stale_sub").exists()

    @pytest.mark.parametrize(
        "stale_kind",
        [
            pytest.param("file_where_dir", id="dst_child_is_file_for_src_dir"),
            pytest.param("dir_where_file", id="dst_child_is_dir_for_src_file"),
            pytest.param("symlink_where_file", id="dst_child_is_symlink_for_src_file"),
        ],
    )
    def test_replaces_dst_child_of_wrong_type(
        self,
        roots: Roots,
        handler: PersistFilesHandler,
        stale_kind: str,
    ):
        """Inside a preserved dir, dst children of the wrong type are replaced."""
        a = make_owned_dir(
            roots.src / "A",
            uid=src_uid("alice"),
            gid=src_gid("agroup"),
            mode=0o755,
        )

        if stale_kind == "file_where_dir":
            # src/A/sub is a dir, but dst already has a regular file there.
            make_owned_dir(
                a / "sub",
                uid=src_uid("bob"),
                gid=src_gid("bgroup"),
                mode=0o700,
            )
            make_owned_file(
                a / "sub" / "inner.txt",
                "I",
                uid=src_uid("carol"),
                gid=src_gid("cgroup"),
                mode=0o600,
            )
            (roots.dst / "A").mkdir()
            (roots.dst / "A" / "sub").write_text("garbage")

            handler.preserve_persist_entry(persist_entry_for(a, roots.src))

            assert (roots.dst / "A" / "sub").is_dir()
            assert not (roots.dst / "A" / "sub").is_symlink()
            assert (roots.dst / "A" / "sub" / "inner.txt").read_text() == "I"

        elif stale_kind == "dir_where_file":
            make_owned_file(
                a / "child",
                "FILE",
                uid=src_uid("bob"),
                gid=src_gid("bgroup"),
                mode=0o644,
            )
            (roots.dst / "A").mkdir()
            (roots.dst / "A" / "child").mkdir()
            (roots.dst / "A" / "child" / "stale").write_text("nope")

            handler.preserve_persist_entry(persist_entry_for(a, roots.src))

            child = roots.dst / "A" / "child"
            assert child.is_file() and not child.is_symlink()
            assert child.read_text() == "FILE"

        else:  # symlink_where_file
            make_owned_file(
                a / "child",
                "FILE",
                uid=src_uid("bob"),
                gid=src_gid("bgroup"),
                mode=0o644,
            )
            (roots.dst / "A").mkdir()
            (roots.dst / "A" / "child").symlink_to("elsewhere")

            handler.preserve_persist_entry(persist_entry_for(a, roots.src))

            child = roots.dst / "A" / "child"
            assert child.is_file() and not child.is_symlink()
            assert child.read_text() == "FILE"


#
# ------------ Parent preparation ------------ #
#
class TestParentPreparation:
    """_prepare_parent decides what to do with each parent on the dst side.

    Behaviors per branch:
      - dst parent already a directory  -> kept as-is (perms NOT overwritten)
      - dst parent is a file or symlink -> unlinked + recreated from src
      - dst parent missing              -> created from src
    """

    def test_existing_dst_parent_dir_is_kept_unchanged(
        self, roots: Roots, handler: PersistFilesHandler
    ):
        # src/A owned by alice/agroup, but dst/A pre-exists with carol/cgroup
        # ids and a *different* mode. Handler must NOT overwrite dst/A's
        # attrs, only place the new entry under it.
        make_owned_dir(
            roots.src / "A",
            uid=src_uid("alice"),
            gid=src_gid("agroup"),
            mode=0o755,
        )
        src_file = make_owned_file(
            roots.src / "A" / "f.txt",
            "x",
            uid=src_uid("bob"),
            gid=src_gid("bgroup"),
            mode=0o644,
        )
        make_owned_dir(
            roots.dst / "A",
            uid=src_uid("carol"),  # raw uid (no mapping applied to dst)
            gid=src_gid("cgroup"),
            mode=0o700,
        )
        pre_attrs = stat_uid_gid_mode(roots.dst / "A")

        handler.preserve_persist_entry(persist_entry_for(src_file, roots.src))

        # dst/A's attrs unchanged.
        assert stat_uid_gid_mode(roots.dst / "A") == pre_attrs
        # entry placed correctly
        assert (roots.dst / "A" / "f.txt").read_text() == "x"

    @pytest.mark.parametrize(
        "stale_kind",
        [
            pytest.param("file", id="dst_parent_is_file"),
            pytest.param("symlink", id="dst_parent_is_symlink"),
        ],
    )
    def test_dst_parent_replaced_when_not_a_directory(
        self,
        roots: Roots,
        handler: PersistFilesHandler,
        stale_kind: str,
    ):
        # src has /A/B/f.txt; dst has /A as a regular file (or symlink).
        # _prepare_parent walks parents of /A/B/f.txt -> [/, /A, /A/B] and
        # must replace the misclassified /A with a directory mirroring src/A.
        make_owned_dir(
            roots.src / "A",
            uid=src_uid("alice"),
            gid=src_gid("agroup"),
            mode=0o755,
        )
        make_owned_dir(
            roots.src / "A" / "B",
            uid=src_uid("bob"),
            gid=src_gid("bgroup"),
            mode=0o711,
        )
        src_file = make_owned_file(
            roots.src / "A" / "B" / "f.txt",
            "deep",
            uid=src_uid("carol"),
            gid=src_gid("cgroup"),
            mode=0o600,
        )
        if stale_kind == "file":
            (roots.dst / "A").write_text("not-a-dir")
        else:
            (roots.dst / "A").symlink_to("elsewhere")

        handler.preserve_persist_entry(persist_entry_for(src_file, roots.src))

        # dst/A is now a directory mirroring src/A.
        assert (roots.dst / "A").is_dir()
        assert not (roots.dst / "A").is_symlink()
        assert_uid_gid_mode(
            roots.dst / "A",
            uid=dst_uid("alice"),
            gid=dst_gid("agroup"),
            mode=0o755,
        )
        # dst/A/B mirrors src/A/B.
        assert_uid_gid_mode(
            roots.dst / "A" / "B",
            uid=dst_uid("bob"),
            gid=dst_gid("bgroup"),
            mode=0o711,
        )
        # dst/A/B/f.txt is the persisted file.
        assert (roots.dst / "A" / "B" / "f.txt").read_text() == "deep"
        assert_uid_gid_mode(
            roots.dst / "A" / "B" / "f.txt",
            uid=dst_uid("carol"),
            gid=dst_gid("cgroup"),
            mode=0o600,
        )


#
# ------------ preserve_persist_entry validation against real fs state ------------ #
#
class TestPreserveValidationOnFs:
    """preserve_persist_entry rejects src that is missing or non-regular."""

    def test_missing_src_raises(self, roots: Roots, handler: PersistFilesHandler):
        with pytest.raises(ValueError, match="doesn't exist"):
            handler.preserve_persist_entry("/does/not/exist")

    def test_special_file_src_raises(self, roots: Roots, handler: PersistFilesHandler):
        fifo = roots.src / "pipe"
        os.mkfifo(fifo)
        assert stat.S_ISFIFO(os.stat(fifo).st_mode)

        with pytest.raises(ValueError, match="must be either a file"):
            handler.preserve_persist_entry(persist_entry_for(fifo, roots.src))


#
# ------------ Idempotence ------------ #
#
class TestIdempotence:
    """Calling preserve_persist_entry twice converges on the same state."""

    @pytest.mark.parametrize(
        "build_entry",
        [
            pytest.param("file", id="file"),
            pytest.param("symlink", id="symlink"),
            pytest.param("broken_symlink", id="broken_symlink"),
            pytest.param("directory", id="directory"),
        ],
    )
    def test_double_call_yields_same_result(
        self,
        roots: Roots,
        handler: PersistFilesHandler,
        build_entry: str,
    ):
        if build_entry == "file":
            entry = make_owned_file(
                roots.src / "f.txt",
                "data",
                uid=src_uid("alice"),
                gid=src_gid("agroup"),
                mode=0o644,
            )
        elif build_entry == "symlink":
            make_owned_file(
                roots.src / "real",
                "data",
                uid=src_uid("alice"),
                gid=src_gid("agroup"),
                mode=0o644,
            )
            entry = make_owned_symlink(
                roots.src / "lnk",
                "real",
                uid=src_uid("bob"),
                gid=src_gid("bgroup"),
            )
        elif build_entry == "broken_symlink":
            entry = make_owned_symlink(
                roots.src / "lnk",
                "missing",
                uid=src_uid("bob"),
                gid=src_gid("bgroup"),
            )
        else:  # directory
            entry = make_owned_dir(
                roots.src / "A",
                uid=src_uid("alice"),
                gid=src_gid("agroup"),
                mode=0o755,
            )
            make_owned_file(
                entry / "child.txt",
                "x",
                uid=src_uid("bob"),
                gid=src_gid("bgroup"),
                mode=0o640,
            )

        persist_entry = persist_entry_for(entry, roots.src)
        handler.preserve_persist_entry(persist_entry)
        snapshot_first = _snapshot_dst(roots.dst)

        handler.preserve_persist_entry(persist_entry)
        snapshot_second = _snapshot_dst(roots.dst)

        assert snapshot_first == snapshot_second


def _snapshot_dst(dst_root: Path) -> dict[str, tuple]:
    """Take a (path, kind, contents-or-target, uid, gid, mode) snapshot of dst.

    Used to assert idempotence: two snapshots taken before and after a
    redundant preserve call must compare equal.
    """
    out: dict[str, tuple] = {}
    for cur_dir, dnames, fnames in os.walk(dst_root, followlinks=False):
        cur = Path(cur_dir)
        rel = str(cur.relative_to(dst_root)) or "."
        st = os.stat(cur, follow_symlinks=False)
        out[rel] = ("dir", None, st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode))
        for n in fnames + dnames:
            p = cur / n
            rel_n = str(p.relative_to(dst_root))
            lst = os.lstat(p)
            mode = stat.S_IMODE(lst.st_mode)
            if stat.S_ISLNK(lst.st_mode):
                out[rel_n] = ("symlink", os.readlink(p), lst.st_uid, lst.st_gid, mode)
            elif stat.S_ISREG(lst.st_mode):
                out[rel_n] = (
                    "file",
                    p.read_bytes(),
                    lst.st_uid,
                    lst.st_gid,
                    mode,
                )
    return out
