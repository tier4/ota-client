import os
from pathlib import Path
from tests.test_ota_metadata import DIRS_FNAME
import pytest
import subprocess

from tests.ota_client_params import PRIVATE_PEM, POLICY_JSON, ECU_INFO

@pytest.fixture(autouse=True)
def custome_test_configs(tmp_path: Path):
    import configs

    tmp_dir = tmp_path / "tmp"
    cfg = configs.get_empty_conf()
    cfg.TMP_DIR = tmp_dir

    return cfg

def test__file_sha256():
    import ota_client
    import shlex

    file_path = __file__
    cmd = f"sha256sum {file_path}"
    result = subprocess.check_output(shlex.split(cmd))
    assert ota_client._file_sha256(file_path) == shlex.split(result.decode("utf-8"))[0]


@pytest.mark.parametrize(
    "regular_inf_entry, mode, uid, gid, nlink, hash, path",
    [
        (
            "0644,0,0,1,1d0f3bebaa884e594ace543fb936a0db41c3092e59891427e9aa67126d3885c1,'/usr/share/i18n/charmaps/ISO_8859-1,GL.gz'",
            int("0644", 8),
            0,
            0,
            1,
            "1d0f3bebaa884e594ace543fb936a0db41c3092e59891427e9aa67126d3885c1",
            Path("/usr/share/i18n/charmaps/ISO_8859-1,GL.gz"),
        ),
        (
            "0755,0,0,3,aea949a9f55c8655953bb921ceb4b7ba92a9d218bcacbc178005baf7ee1450d3,'/usr/bin/bzcat'",
            int("0755", 8),
            0,
            0,
            3,
            "aea949a9f55c8655953bb921ceb4b7ba92a9d218bcacbc178005baf7ee1450d3",
            Path("/usr/bin/bzcat"),
        ),
    ],
)
def test_RegularInf(regular_inf_entry, mode, uid, gid, nlink, hash, path):
    from ota_client import RegularInf

    parsed = RegularInf(regular_inf_entry)

    assert parsed.mode == mode
    assert parsed.uid == uid
    assert parsed.gid == gid
    assert parsed.nlink == nlink
    assert parsed.sha256hash == hash
    assert parsed.path == path


@pytest.mark.parametrize(
    "entry, mode, uid, gid, path",
    [
        (
            r"0755,0,0,'/usr/lib/python3/dist-packages/ansible/modules/network/layer3'",
            int("0755", 8),
            0,
            0,
            Path(r"/usr/lib/python3/dist-packages/ansible/modules/network/layer3"),
        )
    ],
)
def test_DirectoryInf(entry, mode, uid, gid, path):
    from ota_client import DirectoryInf

    parsed = DirectoryInf(entry)

    assert parsed.mode == mode
    assert parsed.uid == uid
    assert parsed.gid == gid
    assert parsed.path == path


@pytest.mark.parametrize(
    "entry, mode, uid, gid, link, target",
    [
        (
            r"0777,0,0,'/usr/lib/gvfs/gvfsd','../../libexec/gvfsd'",
            int("0777", 8),
            0,
            0,
            Path(r"/usr/lib/gvfs/gvfsd"),
            Path(r"../../libexec/gvfsd"),
        ),
        (
            r"0777,0,0,'/var/lib/ieee-data/iab.csv','/usr/share/ieee-data/'\'','\''iab.csv'",
            int("0777", 8),
            0,
            0,
            Path(r"/var/lib/ieee-data/iab.csv"),
            Path(r"/usr/share/ieee-data/','iab.csv"),
        ),
    ],
)
def test_SymbolicLinkInf(entry, mode, uid, gid, link, target):
    from ota_client import SymbolicLinkInf

    parsed = SymbolicLinkInf(entry)

    assert parsed.mode == mode
    assert parsed.uid == uid
    assert parsed.gid == gid
    assert parsed.slink == link
    assert parsed.srcpath == target


def _assert_own(entry: Path, uid, gid):
    st = entry.lstat()
    assert st.st_uid == uid
    assert st.st_gid == gid


def test__copy_complete(tmp_path: Path):
    import ota_client

    src = tmp_path / "src"
    src_A_B = tmp_path / "src/A/B/"
    src_A_B.mkdir(parents=True)

    src_A_B_a = src_A_B / "a"
    src_A_B_a.write_text("a")

    os.chown(src, 1234, 4321)
    os.chown(src / "A", 2345, 5432)
    os.chown(src / "A/B", 3456, 6543)
    os.chown(src / "A/B/a", 4567, 7654)

    dst = tmp_path / "dst"
    dest_file = dst / "A/B/a"
    ota_client._copy_complete(src_A_B_a, dest_file)

    """
    output = subprocess.check_output(["ls", "-lR", tmpdir.join("dst")])
    print(output.decode("utf-8"))
    """
    assert (dst).is_dir()
    assert (dst / "A").is_dir()
    assert (dst / "A/B").is_dir()
    assert (dst / "A/B/a").is_file()

    _assert_own(dst, 1234, 4321)
    _assert_own(dst / "A", 2345, 5432)
    _assert_own(dst / "A/B", 3456, 6543)
    _assert_own(dst / "A/B/a", 4567, 7654)


def test__copy_complete_symlink_doesnot_exist(tmp_path: Path):
    import ota_client

    src = tmp_path / "src"
    src.mkdir()

    src_a = src / "a"
    src_a.symlink_to("doesnotexist")  # src_a -> doesnotexist

    dst = tmp_path / "dst"  # dst shouldn't exist

    os.chown(src, 1234, 4321)
    os.chown(src_a, 2345, 5432, follow_symlinks=False)

    dst_a = dst / "a"
    ota_client._copy_complete(src_a, dst_a)

    """
    output = subprocess.check_output(["ls", "-lR", tmpdir.join("dst")])
    print(output.decode("utf-8"))
    """
    assert dst.is_dir()
    assert os.readlink((dst / "a")) == "doesnotexist"

    _assert_own(dst, 1234, 4321)
    _assert_own(dst / "a", 2345, 5432)


def test__copytree_complete(tmp_path: Path):
    import ota_client

    src = tmp_path / "src"
    src.mkdir()

    src_A = src / "A"
    src_A.mkdir()

    src_A_a = src_A / "a"
    src_A_a.symlink_to("doesnotexist")  # src_a -> doesnotexist

    src_A_B = src_A / "B"
    src_A_B.mkdir()

    src_A_b = src_A / "b"
    src_A_b.symlink_to("B")  # b -> B

    src_A_B_C = src_A_B / "C"
    src_A_B_C.mkdir()

    src_A_B_c = src_A_B / "c"
    src_A_B_c.write_text("c")

    src_A_B_d = src_A_B / "d"
    src_A_B_d.symlink_to("c")  # d -> c

    dst = tmp_path / "dst"

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

    assert (tmp_path / "dst").is_dir()
    assert (tmp_path / "dst/A").is_dir()
    assert (tmp_path / "dst/A/a").is_symlink()
    assert os.readlink((tmp_path / "dst/A/a")) == "doesnotexist"
    assert (tmp_path / "dst/A/B").is_dir()
    assert (tmp_path / "dst/A/b").is_symlink()  # "b" is symlink to "B" directory.
    assert os.readlink(tmp_path / "dst/A/b") == "B"
    assert (tmp_path / "dst/A/B/C").is_dir()
    assert (tmp_path / "dst/A/B/c").is_file()
    assert (tmp_path / "dst/A/B/d").is_symlink()
    assert os.readlink(tmp_path / "dst/A/B/d") == "c"

    _assert_own((tmp_path / "dst"), 1234, 4321)
    _assert_own((tmp_path / "dst/A"), 2345, 5432)
    _assert_own((tmp_path / "dst/A/B"), 3456, 6543)
    _assert_own((tmp_path / "dst/A/B/C"), 4567, 7654)
    _assert_own((tmp_path / "dst/A/B/c"), 5678, 8765)


def test_otaclient_read_ecuid(tmp_path: Path):
    import ota_client

    ecuid_path = tmp_path / "ecuid"
    ecuid = """1\n"""
    ecuid_path.write_text(ecuid)
    assert ota_client._read_ecuid(ecuid_path) == "1"


def test_otaclient_read_ecu_info(tmp_path: Path):
    import ota_client

    ecuinfo_path = tmp_path / "ecuinfo.yaml"
    ecuinfo = """\
main_ecu:
  ecu_name: 'autoware_ecu' 
  ecu_type: 'autoware'
  ecu_id: '1'
  version: '0.0.0'
  independent: True
  ip_addr: ''
"""
    ecuinfo_path.write_text(ecuinfo)
    rd_ecuinfo = ota_client._read_ecu_info(ecuinfo_path)
    assert rd_ecuinfo["main_ecu"]["ecu_name"] == "autoware_ecu"
    assert rd_ecuinfo["main_ecu"]["ecu_type"] == "autoware"
    assert rd_ecuinfo["main_ecu"]["ecu_id"] == "1"
    assert rd_ecuinfo["main_ecu"]["version"] == "0.0.0"
    assert rd_ecuinfo["main_ecu"]["independent"] == True
    assert rd_ecuinfo["main_ecu"]["ip_addr"] == ""


def test__cleanup_dir(tmp_path: Path):
    import ota_client

    clean_dir_path = tmp_path / "cleantest"
    clean_dir_path.mkdir()

    cld_a = clean_dir_path / "a"
    cld_a.write_text("a")
    cld_b = clean_dir_path / "b"
    cld_b.write_text("b")
    cld_c = clean_dir_path / "c"
    cld_c.write_text("c")
    cldA = clean_dir_path / "A"
    cldA.mkdir()

    cldA_d = cldA / "d"
    cldA_d.write_text("d")

    cldB = clean_dir_path / "B"
    cldB.mkdir()

    cldB_d = cldA / "e"
    cldB_d.write_text("e")

    cldC = clean_dir_path / "C"
    cldC.mkdir()

    cldC_f = cldA / "f"
    cldC_f.write_text("d")

    assert clean_dir_path.is_dir()
    assert cld_a.is_file()
    assert cld_b.is_file()
    assert cld_c.is_file()
    assert cldA.is_dir()
    assert cldB.is_dir()
    assert cldC.is_dir()

    ota_client._cleanup_dir(clean_dir_path)

    assert clean_dir_path.is_dir()
    assert not cld_a.is_file()
    assert not cld_b.is_file()
    assert not cld_a.is_file()
    assert not cldA.is_dir()
    assert not cldB.is_dir()
    assert not cldC.is_dir()


def test__gen_directories(tmp_path: Path):
    import ota_client

    DIR_LIST_DATA = """\
1777,0,0,'/A'
0700,0,0,'/B'
0755,0,0,'/C'
2750,0,30,'/C/D'
0755,0,0,'/C/E'
0755,0,0,'/C/E/F'
"""

    src = tmp_path / "src"
    src.mkdir()

    dirs_file = src / "dirs.txt"
    dirs_file.write_text(DIR_LIST_DATA)

    dst = tmp_path / "dst"
    dst.mkdir()

    ota_client._gen_directories(dirs_file, dst)
    assert (dst / "A").is_dir()
    assert (dst / "B").is_dir()
    assert (dst / "C").is_dir()
    assert (dst / "C/D").is_dir()
    assert (dst / "C/E").is_dir()
    assert (dst / "C/E/F").is_dir()


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
def test__copy_persistent(tmp_path: Path, persistent, result, type):
    import ota_client

    src = tmp_path / "src"
    dst = tmp_path / "dst"

    src_A = src / "A"
    src_B = src / "B"
    src_A.mkdir(parents=True)
    src_B.mkdir(parents=True)

    src_B_a = src_B / "a"
    src_B_a.write_text("a")

    src_C = src / "C"
    src_C_D = src_C / "D"
    src_C_D_E = src_C_D / "E"
    src_C_D_E.mkdir(parents=True)

    src_C_D_E_a = src_C_D_E / "a"
    src_C_D_E_a.write_text("a")
    src_C_D_a = src_C_D / "a"
    src_C_D_a.symlink_to("E/a")

    src_path = src / Path(persistent).relative_to("/")
    ota_client._copy_persistent(src_path, dst)
    dst_path: Path = dst / src.relative_to("/") / Path(result).relative_to("/")
    print(f"src: {src_path} dst: {dst_path}")
    if type == "dir":
        assert dst_path.is_dir()
    elif type == "file":
        assert dst_path.is_file()
    if type == "symlink":
        assert dst_path.is_symlink()


def test__gen_persistent_files(tmp_path: Path):
    import ota_client

    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()

    src_A = src / "A"
    src_B = src / "B"
    src_A.mkdir()
    src_B.mkdir()

    src_B_a = src_B / "a"
    src_B_a.write_text("a")

    src_C = src / "C"
    src_C.mkdir()
    src_C_D = src_C / "D"
    src_C_D_E = src_C_D / "E"
    src_C_D_E.mkdir(parents=True)
    src_C_D_E_a = src_C_D_E / "a"
    src_C_D_E_a.write_text("a")
    src_C_D_a = src_C_D / "a"
    src_C_D_a.symlink_to("E/a")

    PERSISTENT_DATA = f"'{str(src)}/A'\n'{str(src)}/B'\n'{str(src)}/C/D'\n"
    persistent_file = tmp_path.joinpath("persistent.txt")
    persistent_file.write_text(PERSISTENT_DATA)

    ota_client._gen_persistent_files(persistent_file, dst)

    assert (dst / src / "A").is_dir()
    assert (dst / src / "B").is_dir()
    assert (dst / src_B_a).is_file()
    assert (dst / src_C).is_dir()
    assert (dst / src_C_D).is_dir()
    assert (dst / src_C_D_E).is_dir()
    assert (dst / src_C_D_E_a).is_file()
    assert (dst / src_C_D_a).is_symlink()


def test__header_str_to_dict(tmp_path: Path):
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


def test__save_update_ecuinfo(tmp_path: Path):
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
    assert ecuinfo_yaml_path.is_file()

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
