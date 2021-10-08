import os
import pytest
from pathlib import Path


def test_ota_client_cp_parents_perserve_src_is_dir(mocker, tmp_path):
    import ota_client

    dst = tmp_path / "dst"
    dst.mkdir()

    src = tmp_path / "src"
    src.mkdir()

    """
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

    ota_client.cp_parents(B, dst)

    # src/A
    assert (dst / A.relative_to("/")).is_dir()
    assert not (dst / A.relative_to("/")).is_symlink()

    # src/A/B/
    assert (dst / B.relative_to("/")).is_dir()
    assert not (dst / B.relative_to("/")).is_symlink()

    # src/A/B/c
    assert (dst / c.relative_to("/")).is_file()
    assert not (dst / c.relative_to("/")).is_symlink()

    # src/A/B/to_c
    assert (dst / to_c.relative_to("/")).is_file()
    assert (dst / to_c.relative_to("/")).is_symlink()

    # src/A/B/to_broken_c
    assert not (dst / to_broken_c.relative_to("/")).is_file()
    assert (dst / to_broken_c.relative_to("/")).is_symlink()

    # src/A/B/C/
    assert (dst / C.relative_to("/")).is_dir()
    assert not (dst / C.relative_to("/")).is_symlink()

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


def test_ota_client_cp_parents_perserve_src_is_file(mocker, tmp_path):
    import ota_client

    dst = tmp_path / "dst"
    dst.mkdir()

    src = tmp_path / "src"
    src.mkdir()

    """
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

    ota_client.cp_parents(to_b, dst)

    # src/A
    assert (dst / A.relative_to("/")).is_dir()
    assert not (dst / A.relative_to("/")).is_symlink()

    # src/A/to_b (src/A/b is not copied, so to_b.is_file() is False)
    assert not (dst / to_b.relative_to("/")).is_file()
    assert (dst / to_b.relative_to("/")).is_symlink()

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
