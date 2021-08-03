import os
from _pytest import mark
import pytest
import subprocess

from tests.ota_client_params import PRIVATE_PEM, POLICY_JSON, ECU_INFO


@pytest.mark.parametrize(
    "name, decapsuled",
    [
        ("'/A/B/C'", "/A/B/C"),
        ("'/A/ファイル'", "/A/ファイル"),
        ("'/A/'file name''", "/A/'file name'"),
    ],
)
def test__decapsulate(name, decapsuled):
    import ota_client

    assert ota_client._decapsulate(name) == decapsuled


def test__file_sha256():
    import ota_client
    import shlex

    file_path = __file__
    cmd = f"sha256sum {file_path}"
    result = subprocess.check_output(shlex.split(cmd))
    assert ota_client._file_sha256(file_path) == shlex.split(result.decode("utf-8"))[0]


@pytest.mark.parametrize(
    "string, search, num, array, pos",
    [
        ("0755,0,100,'/etc/ssl/certs'", ",", 1, ["0755"], 5),
        ("0755,0,100,'/etc/ssl/certs'", ",", 2, ["0755", "0"], 7),
        ("0755,0,100,'/etc/ssl/certs'", ",", 3, ["0755", "0", "100"], 11),
    ],
)
def test__get_setparated_strings(string, search, num, array, pos):
    import ota_client

    arr, curr = ota_client._get_separated_strings(string, search, num)
    assert arr == array
    assert curr == pos


def _assert_own(entry, uid, gid):
    try:
        entry.readlink()
        return
    except Exception:
        pass
    st = os.stat(entry)
    assert st.st_uid == uid
    assert st.st_gid == gid


def test__copy_complete(tmpdir):
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


def test__copy_complete_symlink_doesnot_exist(tmpdir):
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


def test__copytree_complete(tmpdir):
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


def test__cleanup_dir(tmpdir):
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


def test__gen_directories(tmpdir):
    import ota_client

    DIR_LIST_DATA = """\
1777,0,0,'/A'
0700,0,0,'/B'
0755,0,0,'/C'
2750,0,30,'/C/D'
0755,0,0,'/C/E'
0755,0,0,'/C/E/F'
"""

    src = tmpdir.mkdir("src")
    dirs_file = src.join("dirs.txt")
    dirs_file.write(DIR_LIST_DATA)
    dst = tmpdir.mkdir("dst")
    ota_client._gen_directories(str(dirs_file), str(dst))
    assert os.path.isdir(str(dst) + "/A")
    assert os.path.isdir(str(dst) + "/B")
    assert os.path.isdir(str(dst) + "/C")
    assert os.path.isdir(str(dst) + "/C/D")
    assert os.path.isdir(str(dst) + "/C/E")
    assert os.path.isdir(str(dst) + "/C/E/F")


@pytest.mark.parametrize(
    "persistent, result, type",
    [
        ("/A", "/A", "dir"),
        ("/B", "/B", "dir"),
        ("/C/D", "/C", "dir"),
        ("/C/D", "/C/D", "dir"),
        ("/C/D", "/C/D/E", "dir"),
        ("/C/D", "/C/D/E/a", "file"),
        ("/C/D", "/C/D/a", "symlink"),
    ],
)
def test__copy_persistent(tmpdir, persistent, result, type):
    import ota_client

    src = tmpdir.mkdir("src")
    dst = tmpdir.mkdir("dst")
    src_A = src.mkdir("A")
    src_B = src.mkdir("B")
    src_B_a = src_B.join("a")
    src_B_a.write("a")
    src_C = src.mkdir("C")
    src_C_D = src_C.mkdir("D")
    src_C_D_E = src_C_D.mkdir("E")
    src_C_D_E_a = src_C_D_E.join("a")
    src_C_D_E_a.write("a")
    src_C_D_a = src_C_D.join("a")
    src_C_D_a.mksymlinkto("E/a")

    src_path = src.join(persistent)
    ota_client._copy_persistent(str(src_path), str(dst))
    dst_path = dst.join(src).join(result)
    print(f"src: {src_path} dst: {dst_path}")
    if type == "dir":
        assert os.path.isdir(str(dst_path))
    elif type == "file":
        assert os.path.isfile(str(dst_path))
    if type == "symlink":
        assert os.path.islink(str(dst_path))


def test__gen_persistent_files(tmpdir):
    import ota_client

    src = tmpdir.mkdir("src")
    dst = tmpdir.mkdir("dst")
    src_A = src.mkdir("A")
    src_B = src.mkdir("B")
    src_B_a = src_B.join("a")
    src_B_a.write("a")
    src_C = src.mkdir("C")
    src_C_D = src_C.mkdir("D")
    src_C_D_E = src_C_D.mkdir("E")
    src_C_D_E_a = src_C_D_E.join("a")
    src_C_D_E_a.write("a")
    src_C_D_a = src_C_D.join("a")
    src_C_D_a.mksymlinkto("E/a")

    PERSISTENT_DATA = f"'{str(src)}/A'\n'{str(src)}/B'\n'{str(src)}/C/D'\n"
    persistent_file = tmpdir.join("persistent.txt")
    persistent_file.write(PERSISTENT_DATA)

    ota_client._gen_persistent_files(str(persistent_file), str(dst))

    assert os.path.isdir(str(dst) + str(src) + "/A")
    assert os.path.isdir(str(dst) + str(src) + "/B")
    assert os.path.isfile(str(dst) + str(src_B_a))
    assert os.path.isdir(str(dst) + str(src_C))
    assert os.path.isdir(str(dst) + str(src_C_D))
    assert os.path.isdir(str(dst) + str(src_C_D_E))
    assert os.path.isfile(str(dst) + str(src_C_D_E_a))
    assert os.path.islink(str(dst) + str(src_C_D_a))


def test__header_str_to_dict(tmp_path):
    import ota_client
    from OpenSSL import crypto
    import base64

    private_key = crypto.load_privatekey(crypto.FILETYPE_PEM, PRIVATE_PEM)
    policy_str = POLICY_JSON.replace("\n", "").encode()
    policy = (
        base64.b64encode(policy_str)
        .decode()
        .replace("+", "-")
        .replace("/", "~")
        .replace("=", "_")
    )
    signature = crypto.sign(private_key, policy_str, "sha1")
    sign = (
        base64.b64encode(signature)
        .decode()
        .replace("+", "-")
        .replace("/", "~")
        .replace("=", "_")
    )
    key_id = "ABCDEFGHIJKLMN"
    HEADER_STR = (
        "Cookie:"
        + "CloudFront-Policy="
        + policy
        + ";CloudFront-Signature="
        + sign
        + ";CloudFront-Key-Pair-Id="
        + key_id
    )

    # print(HEADER_STR)

    header_dict = ota_client._header_str_to_dict(HEADER_STR)
    print(f"{header_dict}")
    assert (
        header_dict["Cookie"]
        == "CloudFront-Policy="
        + policy
        + ";CloudFront-Signature="
        + sign
        + ";CloudFront-Key-Pair-Id="
        + key_id
    )


def test__save_update_ecuinfo(tmp_path):
    import ota_client
    import yaml

    ecuinfo_yaml_path = tmp_path / "ecuinfo.yaml"
    ecu_info = {
        "main_ecu": {
            "ecu_name": "Autoware ECU",
            "ecu_type": "autoware",
            "ecu_id": "1",
            "version": "0.1.0",
            "independent": True,
            "ip_addr": "",
        },
        "sub_ecus": [
            {
                "ecu_name": "Perception ECU",
                "ecu_type": "perception",
                "ecu_id": "2",
                "version": "0.2.0",
                "independent": True,
                "ip_addr": "",
            },
            {
                "ecu_name": "Logging ECU",
                "ecu_type": "log",
                "ecu_id": "3",
                "version": "0.3.0",
                "independent": True,
                "ip_addr": "",
            },
        ],
    }

    ota_client._save_update_ecuinfo(ecuinfo_yaml_path, ecu_info)
    assert os.path.isfile(ecuinfo_yaml_path)

    with open(ecuinfo_yaml_path, "r") as f:
        rd_ecuinfo = yaml.load(f, Loader=yaml.SafeLoader)
        assert rd_ecuinfo["main_ecu"]["ecu_name"] == "Autoware ECU"
        assert rd_ecuinfo["main_ecu"]["ecu_type"] == "autoware"
        assert rd_ecuinfo["main_ecu"]["ecu_id"] == "1"
        assert rd_ecuinfo["main_ecu"]["version"] == "0.1.0"
        assert rd_ecuinfo["main_ecu"]["independent"] == True
        assert rd_ecuinfo["sub_ecus"][0]["ecu_name"] == "Perception ECU"
        assert rd_ecuinfo["sub_ecus"][0]["ecu_type"] == "perception"
        assert rd_ecuinfo["sub_ecus"][0]["ecu_id"] == "2"
        assert rd_ecuinfo["sub_ecus"][0]["version"] == "0.2.0"
        assert rd_ecuinfo["sub_ecus"][0]["independent"] == True
        assert rd_ecuinfo["sub_ecus"][1]["ecu_name"] == "Logging ECU"
        assert rd_ecuinfo["sub_ecus"][1]["ecu_type"] == "log"
        assert rd_ecuinfo["sub_ecus"][1]["ecu_id"] == "3"
        assert rd_ecuinfo["sub_ecus"][1]["version"] == "0.3.0"
        assert rd_ecuinfo["sub_ecus"][1]["independent"] == True
