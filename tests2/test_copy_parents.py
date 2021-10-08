import os
import stat
import pytest
from pathlib import Path


def create_files(tmp_path):
    dst = tmp_path / "dst"
    dst.mkdir()

    src = tmp_path / "src"
    src.mkdir()

    """
    src/
    src/a
    src/to_a -> a
    src/to_broken_a -> broken_a
    src/A
    src/A/b
    src/A/to_b -> b
    src/A/to_broken_b -> broken_b
    src/A/B/
    src/A/B/c
    src/A/B/to_c -> c
    src/A/B/to_broken_c -> broken_c
    src/A/B/C/
    """

    a = src / "a"
    a.write_text("a")
    to_a = src / "to_a"
    to_a.symlink_to("a")
    to_broken_a = src / "to_broken_a"
    to_broken_a.symlink_to("broken_a")
    A = src / "A"
    A.mkdir()

    b = A / "b"
    b.write_text("b")
    to_b = A / "to_b"
    to_b.symlink_to("b")
    to_broken_b = A / "to_broken_b"
    to_broken_b.symlink_to("broken_b")
    B = A / "B"
    B.mkdir()

    c = B / "c"
    c.write_text("c")
    to_c = B / "to_c"
    to_c.symlink_to("c")
    to_broken_c = B / "to_broken_c"
    to_broken_c.symlink_to("broken_c")
    C = B / "C"
    C.mkdir()

    os.chown(src, 10, 20, follow_symlinks=False)
    os.chown(a, 30, 40, follow_symlinks=False)
    os.chown(to_a, 50, 60, follow_symlinks=False)
    os.chown(to_broken_a, 70, 80, follow_symlinks=False)
    os.chown(A, 90, 100, follow_symlinks=False)
    os.chown(b, 110, 120, follow_symlinks=False)
    os.chown(to_b, 130, 140, follow_symlinks=False)
    os.chown(to_broken_b, 150, 160, follow_symlinks=False)
    os.chown(B, 170, 180, follow_symlinks=False)
    os.chown(c, 190, 200, follow_symlinks=False)
    os.chown(to_c, 210, 220, follow_symlinks=False)
    os.chown(to_broken_c, 230, 240, follow_symlinks=False)
    os.chown(C, 250, 260, follow_symlinks=False)

    os.chmod(src, 0o111)
    os.chmod(a, 0o112)
    # os.chmod(to_a, 0o113)
    # os.chmod(to_broken_a, 0o114)
    os.chmod(A, 0o115)
    os.chmod(b, 0o116)
    # os.chmod(to_b, 0o117)
    # os.chmod(to_broken_b, 0o121)
    os.chmod(B, 0o122)
    os.chmod(c, 0o123)
    # os.chmod(to_c, 0o124)
    # os.chmod(to_broken_c, 0o125)
    os.chmod(C, 0o126)

    return (
        dst,
        src,
        a,
        to_a,
        to_broken_a,
        A,
        b,
        to_b,
        to_broken_b,
        B,
        c,
        to_c,
        to_broken_c,
        C,
    )


def assert_uid_gid_mode(path, uid, gid, mode):
    st = os.stat(path, follow_symlinks=False)
    assert st[stat.ST_UID] == uid
    assert st[stat.ST_GID] == gid
    assert stat.S_IMODE(st[stat.ST_MODE]) == mode


def test_copy_parents_copy_parents_src_dir(mocker, tmp_path):
    from copy_parents import copy_parents

    (
        dst,
        src,
        a,
        to_a,
        to_broken_a,
        A,
        b,
        to_b,
        to_broken_b,
        B,
        c,
        to_c,
        to_broken_c,
        C,
    ) = create_files(tmp_path)

    copy_parents(B, dst)

    # src/A
    assert (dst / A.relative_to("/")).is_dir()
    assert not (dst / A.relative_to("/")).is_symlink()
    assert_uid_gid_mode(dst / A.relative_to("/"), 90, 100, 0o115)

    # src/A/B/
    assert (dst / B.relative_to("/")).is_dir()
    assert not (dst / B.relative_to("/")).is_symlink()
    assert_uid_gid_mode(dst / B.relative_to("/"), 170, 180, 0o122)

    # src/A/B/c
    assert (dst / c.relative_to("/")).is_file()
    assert not (dst / c.relative_to("/")).is_symlink()
    assert_uid_gid_mode(dst / c.relative_to("/"), 190, 200, 0o123)

    # src/A/B/to_c
    assert (dst / to_c.relative_to("/")).is_file()
    assert (dst / to_c.relative_to("/")).is_symlink()
    assert_uid_gid_mode(dst / to_c.relative_to("/"), 210, 220, 0o777)

    # src/A/B/to_broken_c
    assert not (dst / to_broken_c.relative_to("/")).is_file()
    assert (dst / to_broken_c.relative_to("/")).is_symlink()
    assert_uid_gid_mode(dst / to_broken_c.relative_to("/"), 230, 240, 0o777)

    # src/A/B/C/
    assert (dst / C.relative_to("/")).is_dir()
    assert not (dst / C.relative_to("/")).is_symlink()
    assert_uid_gid_mode(dst / C.relative_to("/"), 250, 260, 0o126)

    # followings should not exist
    # src/a
    assert not (dst / a.relative_to("/")).exists()
    assert not (dst / a.relative_to("/")).is_symlink()
    # src/to_a
    assert not (dst / to_a.relative_to("/")).exists()
    assert not (dst / to_a.relative_to("/")).is_symlink()
    # src/to_broken_a
    assert not (dst / to_broken_a.relative_to("/")).exists()
    assert not (dst / to_broken_a.relative_to("/")).is_symlink()
    # src/A/b
    assert not (dst / b.relative_to("/")).exists()
    assert not (dst / b.relative_to("/")).is_symlink()
    # src/A/to_b
    assert not (dst / to_b.relative_to("/")).exists()
    assert not (dst / to_b.relative_to("/")).is_symlink()
    # src/A/to_broken_b
    assert not (dst / to_broken_b.relative_to("/")).exists()
    assert not (dst / to_broken_b.relative_to("/")).is_symlink()


def test_copy_parents_copy_parents_src_file(mocker, tmp_path):
    from copy_parents import copy_parents

    (
        dst,
        src,
        a,
        to_a,
        to_broken_a,
        A,
        b,
        to_b,
        to_broken_b,
        B,
        c,
        to_c,
        to_broken_c,
        C,
    ) = create_files(tmp_path)

    copy_parents(to_b, dst)

    # src/A
    assert (dst / A.relative_to("/")).is_dir()
    assert not (dst / A.relative_to("/")).is_symlink()
    assert_uid_gid_mode(dst / A.relative_to("/"), 90, 100, 0o115)

    # src/A/to_b (src/A/b is not copied, so to_b.is_file() is False)
    assert not (dst / to_b.relative_to("/")).is_file()
    assert (dst / to_b.relative_to("/")).is_symlink()
    assert_uid_gid_mode(dst / to_b.relative_to("/"), 130, 140, 0o777)

    # followings should not exist
    # src/a
    assert not (dst / a.relative_to("/")).exists()
    assert not (dst / a.relative_to("/")).is_symlink()
    # src/to_a
    assert not (dst / to_a.relative_to("/")).exists()
    assert not (dst / to_a.relative_to("/")).is_symlink()
    # src/to_broken_a
    assert not (dst / to_broken_a.relative_to("/")).exists()
    assert not (dst / to_broken_a.relative_to("/")).is_symlink()
    # src/A/b
    assert not (dst / b.relative_to("/")).exists()
    assert not (dst / b.relative_to("/")).is_symlink()
    # src/A/to_broken_b
    assert not (dst / to_broken_b.relative_to("/")).exists()
    assert not (dst / to_broken_b.relative_to("/")).is_symlink()
    # src/A/B
    assert not (dst / B.relative_to("/")).exists()
    assert not (dst / B.relative_to("/")).is_symlink()
    # src/A/B/c
    assert not (dst / c.relative_to("/")).exists()
    assert not (dst / c.relative_to("/")).is_symlink()
    # src/A/B/to_c
    assert not (dst / to_c.relative_to("/")).exists()
    assert not (dst / to_c.relative_to("/")).is_symlink()
    # src/A/B/to_broken_c
    assert not (dst / to_broken_c.relative_to("/")).exists()
    assert not (dst / to_broken_c.relative_to("/")).is_symlink()
    # src/A/B/C/
    assert not (dst / C.relative_to("/")).exists()
    assert not (dst / C.relative_to("/")).is_symlink()


def test_copy_parents_copy_parents_B_exists(mocker, tmp_path):
    from copy_parents import copy_parents

    (
        dst,
        src,
        a,
        to_a,
        to_broken_a,
        A,
        b,
        to_b,
        to_broken_b,
        B,
        c,
        to_c,
        to_broken_c,
        C,
    ) = create_files(tmp_path)

    dst_A = dst / tmp_path.relative_to("/") / "src" / "A"
    dst_A.mkdir(parents=True)
    print(f"dst_A {dst_A}")
    dst_B = dst_A / "B"
    dst_B.mkdir()

    os.chown(dst_A, 0, 1, follow_symlinks=False)
    os.chown(dst_B, 1, 2, follow_symlinks=False)
    os.chmod(dst_A, 0o765)
    os.chmod(dst_B, 0o654)
    st = os.stat(dst_A, follow_symlinks=False)

    copy_parents(C, dst)

    # src/A
    assert (dst / A.relative_to("/")).is_dir()
    assert not (dst / A.relative_to("/")).is_symlink()
    assert_uid_gid_mode(dst / A.relative_to("/"), 0, 1, 0o765)

    # src/A/B
    assert (dst / B.relative_to("/")).is_dir()
    assert not (dst / B.relative_to("/")).is_symlink()
    assert_uid_gid_mode(dst / B.relative_to("/"), 1, 2, 0o654)

    # src/A/B/C/
    assert (dst / C.relative_to("/")).is_dir()
    assert not (dst / C.relative_to("/")).is_symlink()
    assert_uid_gid_mode(dst / C.relative_to("/"), 250, 260, 0o126)

    # followings should not exist
    # src/a
    assert not (dst / a.relative_to("/")).exists()
    assert not (dst / a.relative_to("/")).is_symlink()
    # src/to_a
    assert not (dst / to_a.relative_to("/")).exists()
    assert not (dst / to_a.relative_to("/")).is_symlink()
    # src/to_broken_a
    assert not (dst / to_broken_a.relative_to("/")).exists()
    assert not (dst / to_broken_a.relative_to("/")).is_symlink()
    # src/A/b
    assert not (dst / b.relative_to("/")).exists()
    assert not (dst / b.relative_to("/")).is_symlink()
    # src/A/to_b (src/A/b is not copied, so to_b.is_file() is False)
    assert not (dst / to_b.relative_to("/")).exists()
    assert not (dst / to_b.relative_to("/")).is_symlink()
    # src/A/to_broken_b
    assert not (dst / to_broken_b.relative_to("/")).exists()
    assert not (dst / to_broken_b.relative_to("/")).is_symlink()
    # src/A/B/c
    assert not (dst / c.relative_to("/")).exists()
    assert not (dst / c.relative_to("/")).is_symlink()
    # src/A/B/to_c
    assert not (dst / to_c.relative_to("/")).exists()
    assert not (dst / to_c.relative_to("/")).is_symlink()
    # src/A/B/to_broken_c
    assert not (dst / to_broken_c.relative_to("/")).exists()
    assert not (dst / to_broken_c.relative_to("/")).is_symlink()
