import os
import pytest
import subprocess


def _assert_own(entry, uid, gid):
    try:
        entry.readlink()
        return
    except Exception:
        pass
    st = os.stat(entry)
    assert st.st_uid == uid
    assert st.st_gid == gid


def test_ota_client_copy_complete(tmpdir):
    import ota_client

    src = tmpdir.mkdir("src")
    src_A = src.mkdir("A")
    src_A_B = src_A.mkdir("B")
    src_A_B_a = src_A_B.join("a")
    src_A_B_a.write("a")

    dst = tmpdir.join("dst")

    os.chown(src, 1234, 4321)
    os.chown(src_A, 2345, 5432)
    os.chown(src_A_B, 3456, 6543)
    os.chown(src_A_B_a, 4567, 7654)

    dest_file = tmpdir.join("dst/A/B/a")
    ota_client._copy_complete(src_A_B_a, dest_file)

    """
    output = subprocess.check_output(["ls", "-lR", tmpdir.join("dst")])
    print(output.decode("utf-8"))
    """
    assert tmpdir.join("dst").ensure_dir()
    assert tmpdir.join("dst/A").ensure_dir()
    assert tmpdir.join("dst/A/B").ensure_dir()
    assert tmpdir.join("dst/A/B/c").ensure()

    _assert_own(tmpdir.join("dst"), 1234, 4321)
    _assert_own(tmpdir.join("dst/A"), 2345, 5432)
    _assert_own(tmpdir.join("dst/A/B"), 3456, 6543)
    _assert_own(tmpdir.join("dst/A/B/a"), 4567, 7654)


def test_ota_client_copy_complete_symlink_doesnot_exist(tmpdir):
    import ota_client

    src = tmpdir.mkdir("src")
    src_a = src.join("a")
    src_a.mksymlinkto("doesnotexist")  # src_a -> doesnotexist

    dst = tmpdir.join("dst")

    os.chown(src, 1234, 4321)
    os.chown(src_a, 2345, 5432, follow_symlinks=False)

    dst_a = tmpdir.join("dst/a")
    ota_client._copy_complete(src_a, dst_a)

    """
    output = subprocess.check_output(["ls", "-lR", tmpdir.join("dst")])
    print(output.decode("utf-8"))
    """
    assert tmpdir.join("dst").ensure_dir()
    assert tmpdir.join("dst/a").readlink() == "doesnotexist"

    _assert_own(tmpdir.join("dst"), 1234, 4321)
    _assert_own(tmpdir.join("dst/a"), 2345, 5432)


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

    ota_client._copytree_complete(src, dst)

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

    _assert_own(tmpdir.join("dst"), 1234, 4321)
    _assert_own(tmpdir.join("dst/A"), 2345, 5432)
    _assert_own(tmpdir.join("dst/A/B"), 3456, 6543)
    _assert_own(tmpdir.join("dst/A/B/C"), 4567, 7654)
    _assert_own(tmpdir.join("dst/A/B/c"), 5678, 8765)


def test_otaclient_read_ecuid(tmpdir):
    import ota_client

    ecuid_path = tmpdir.join("ecuid")
    ecuid = """1\n"""
    ecuid_path.write(ecuid)
    assert ota_client._read_ecuid(ecuid_path) == "1"


def test_otaclient_read_ecu_info(tmpdir):
    import ota_client

    ecuinfo_path = tmpdir.join("ecuinfo.yaml")
    ecuinfo = """\
main_ecu:
  ecu_name: 'autoware_ecu' 
  ecu_type: 'autoware'
  ecu_id: '1'
  version: '0.0.0'
  independent: True
  ip_addr: ''
"""
    ecuinfo_path.write(ecuinfo)
    rd_ecuinfo = ota_client._read_ecu_info(ecuinfo_path)
    assert rd_ecuinfo["main_ecu"]["ecu_name"] == "autoware_ecu"
    assert rd_ecuinfo["main_ecu"]["ecu_type"] == "autoware"
    assert rd_ecuinfo["main_ecu"]["ecu_id"] == "1"
    assert rd_ecuinfo["main_ecu"]["version"] == "0.0.0"
    assert rd_ecuinfo["main_ecu"]["independent"] == True
    assert rd_ecuinfo["main_ecu"]["ip_addr"] == ""


def test_ota_client_cleanup_dir(tmpdir):
    import ota_client

    clean_dir_path = tmpdir.mkdir("cleantest")
    cld_a = clean_dir_path.join("a")
    cld_a.write("a")
    cld_b = clean_dir_path.join("b")
    cld_b.write("b")
    cld_c = clean_dir_path.join("c")
    cld_c.write("c")
    cldA = clean_dir_path.mkdir("A")
    cldA_d = cldA.join("d")
    cldA_d.write("d")
    cldB = clean_dir_path.mkdir("B")
    cldB_d = cldA.join("e")
    cldB_d.write("e")
    cldC = clean_dir_path.mkdir("C")
    cldC_f = cldA.join("f")
    cldC_f.write("d")

    assert clean_dir_path.ensure_dir()
    assert cld_a.ensure()
    assert cld_b.ensure()
    assert cld_c.ensure()
    assert cldA.ensure_dir()
    assert cldB.ensure_dir()
    assert cldC.ensure_dir()

    ota_client._cleanup_dir(clean_dir_path)

    assert os.path.exists(str(clean_dir_path))
    assert not os.path.isfile(str(cld_a))
    assert not os.path.isfile(str(cld_b))
    assert not os.path.isfile(str(cld_a))
    assert not os.path.exists(str(cldA))
    assert not os.path.exists(str(cldB))
    assert not os.path.exists(str(cldC))
