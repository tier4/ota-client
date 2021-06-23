import os
import pytest
import subprocess


def test_ota_client_copytree_complete(tmpdir):
    import ota_client

    src = tmpdir.mkdir("src")
    src_A = src.mkdir("A")
    src_a = src_A.join("a")
    src_a.mksymlinkto("doesnotexist")  # src_a -> doesnotexist

    src_A_B = src_A.mkdir("B")
    src_A_b = src_A.join("b")
    src_A_b.mksymlinkto("B")  # b -> B

    src_A_B_C = src_A_B.mkdir("C")
    src_A_B_c = src_A_B.join("c")
    src_A_B_c.write("c")
    src_A_B_d = src_A_B.join("d")
    src_A_B_d.mksymlinkto("c")  # d -> c

    dst = tmpdir.join("dst")

    os.chown(src, 1234, 4321)
    os.chown(src_A, 2345, 5432)
    os.chown(src_A_B, 3456, 6543)
    os.chown(src_A_B_C, 4567, 7654)
    os.chown(src_A_B_c, 5678, 8765)

    app.ota_client._copytree_complete(src, dst)

    """
    output = subprocess.check_output(["ls", "-lR", tmpdir.join("dst")])
    print(output.decode("utf-8"))
    """

    assert tmpdir.join("dst").ensure_dir()
    assert tmpdir.join("dst/A").ensure_dir()
    assert tmpdir.join("dst/A/a").ensure()
    assert tmpdir.join("dst/A/a").readlink() == "doesnotexist"
    assert tmpdir.join("dst/A/B").ensure_dir()
    assert tmpdir.join("dst/A/b").ensure_dir()  # "b" is symlink to "B" directory.
    assert tmpdir.join("dst/A/b").readlink() == "B"
    assert tmpdir.join("dst/A/B/C").ensure_dir()
    assert tmpdir.join("dst/A/B/c").ensure()
    assert tmpdir.join("dst/A/B/d").ensure()
    assert tmpdir.join("dst/A/B/d").readlink() == "c"

    def assert_own(entry, uid, gid):
        try:
            entry.readlink()
            return
        except Exception:
            pass
        st = os.stat(entry)
        assert st.st_uid == uid
        assert st.st_gid == gid

    assert_own(tmpdir.join("dst"), 1234, 4321)
    assert_own(tmpdir.join("dst/A"), 2345, 5432)
    assert_own(tmpdir.join("dst/A/B"), 3456, 6543)
    assert_own(tmpdir.join("dst/A/B/C"), 4567, 7654)
    assert_own(tmpdir.join("dst/A/B/c"), 5678, 8765)
