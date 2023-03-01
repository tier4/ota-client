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


import pytest
import base64
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from otaclient.app.ota_metadata import (
    _MetadataJWTParser,
    parse_dirs_from_txt,
    parse_persistents_from_txt,
    parse_regulars_from_txt,
    parse_symlinks_from_txt,
)


HEADER = """\
{"alg": "ES256"}\
"""

PAYLOAD = """\
[\
{"version": 1}, \
{"directory": "dirs.txt", "hash": "43afbd19eab7c9e27f402a3332c38d072a69c7932fb35c32c1fc7069695235f1"}, \
{"symboliclink": "symlinks.txt", "hash": "6643bf896d3ac3bd4034d742fae0d7eb82bd384062492235404114aeb34efd7d"}, \
{"regular": "regulars.txt", "hash": "a390f92fe49b8402a2bb9f0b594e9de70f70dc6bf429031ac4d0b21365251600"}, \
{"persistent": "persistents.txt", "hash": "3195ded730474d0181257204ba0fd79766721ab62ace395f20decd44983cb2d3"}, \
{"rootfs_directory": "data"}, \
{"certificate": "ota-intermediate.pem", "hash": "24c0c9ea292458398f05b9b2a31b483c45d27e284743a3f0c7963e2ac0c62ed2"}\
]\
"""

PAYLOAD_W_TOTAL_SIZE = """\
[\
{"version": 1}, \
{"directory": "dirs.txt", "hash": "43afbd19eab7c9e27f402a3332c38d072a69c7932fb35c32c1fc7069695235f1"}, \
{"symboliclink": "symlinks.txt", "hash": "6643bf896d3ac3bd4034d742fae0d7eb82bd384062492235404114aeb34efd7d"}, \
{"regular": "regulars.txt", "hash": "a390f92fe49b8402a2bb9f0b594e9de70f70dc6bf429031ac4d0b21365251600"}, \
{"persistent": "persistents.txt", "hash": "3195ded730474d0181257204ba0fd79766721ab62ace395f20decd44983cb2d3"}, \
{"rootfs_directory": "data"}, \
{"certificate": "ota-intermediate.pem", "hash": "24c0c9ea292458398f05b9b2a31b483c45d27e284743a3f0c7963e2ac0c62ed2"}, \
{"total_regular_size": "108708870"}\
]\
"""


DIR_FNAME = "dirs.txt"
DIR_HASH = "43afbd19eab7c9e27f402a3332c38d072a69c7932fb35c32c1fc7069695235f1"
DIR_INFO = {"file": DIR_FNAME, "hash": DIR_HASH}

SYMLINK_FNAME = "symlinks.txt"
SYMLINK_HASH = "6643bf896d3ac3bd4034d742fae0d7eb82bd384062492235404114aeb34efd7d"
SYMLINK_INFO = {"file": SYMLINK_FNAME, "hash": SYMLINK_HASH}

REGULAR_FNAME = "regulars.txt"
REGULAR_HASH = "a390f92fe49b8402a2bb9f0b594e9de70f70dc6bf429031ac4d0b21365251600"
REGULAR_INFO = {"file": REGULAR_FNAME, "hash": REGULAR_HASH}

PERSISTENT_FNAME = "persistents.txt"
PERSISTENT_HASH = "3195ded730474d0181257204ba0fd79766721ab62ace395f20decd44983cb2d3"
PERSISTENT_INFO = {"file": PERSISTENT_FNAME, "hash": PERSISTENT_HASH}

ROOTFS_DIR = "data"
ROOTFS_DIR_INFO = ROOTFS_DIR

CERTIFICATE_FNAME = "ota-intermediate.pem"
CERTIFICATE_HASH = "24c0c9ea292458398f05b9b2a31b483c45d27e284743a3f0c7963e2ac0c62ed2"
CERTIFICATE_INFO = {"file": CERTIFICATE_FNAME, "hash": CERTIFICATE_HASH}

TOTAL_REGULAR_SIZE = 108708870


def sign(sign_key_file, data: str):
    with open(sign_key_file, "rb") as f:
        priv = serialization.load_pem_private_key(f.read(), password=None)
    return urlsafe_b64encode(
        priv.sign(  # type: ignore
            data.encode(),
            ec.ECDSA(hashes.SHA256()),  # type: ignore
        ),
    )


def urlsafe_b64encode(data):
    if type(data) is str:
        data = data.encode()
    return base64.urlsafe_b64encode(data).decode()


def generate_jwt(payload_str: str, test_dir: Path):
    sign_key_file = test_dir / "keys" / "sign.key"

    header = urlsafe_b64encode(HEADER)
    payload = urlsafe_b64encode(payload_str)
    signature = sign(sign_key_file, f"{header}.{payload}")
    return f"{header}.{payload}.{signature}"


@pytest.fixture
def dir_test() -> Path:
    return Path(__file__).parent


@pytest.mark.parametrize("payload_str", [(PAYLOAD), (PAYLOAD_W_TOTAL_SIZE)])
def test_ota_metadata(dir_test: Path, payload_str: str):
    certs_dir = dir_test / "keys"
    sign_pem = dir_test / "keys" / "sign.pem"

    metadata_jwt = generate_jwt(payload_str, dir_test)
    parser = _MetadataJWTParser(metadata_jwt, certs_dir=str(certs_dir))
    metadata = parser.get_otametadata()
    assert metadata.directory.asdict() == DIR_INFO
    assert metadata.symboliclink.asdict() == SYMLINK_INFO
    assert metadata.regular.asdict() == REGULAR_INFO
    assert metadata.persistent.asdict() == PERSISTENT_INFO
    assert metadata.certificate.asdict() == CERTIFICATE_INFO
    assert metadata.rootfs_directory == ROOTFS_DIR_INFO

    parser.verify_metadata(sign_pem.read_bytes())
    if "total_regular_size" in payload_str:
        assert metadata.total_regular_size == TOTAL_REGULAR_SIZE
    else:
        assert metadata.total_regular_size is None


@pytest.mark.parametrize("payload_str", [(PAYLOAD), (PAYLOAD_W_TOTAL_SIZE)])
def test_ota_metadata_exception(dir_test: Path, payload_str):
    certs_dir = dir_test / "keys"
    sign_pem = dir_test / "keys" / "sign.pem"

    metadata_jwt = generate_jwt(payload_str, dir_test)
    parser = _MetadataJWTParser(metadata_jwt, certs_dir=str(certs_dir))
    with pytest.raises(ValueError):
        # sing.key is not a valid cert
        sign_pem = dir_test / "keys" / "sign.key"
        parser.verify_metadata(sign_pem.read_bytes())


@pytest.mark.parametrize("payload_str", [(PAYLOAD), (PAYLOAD_W_TOTAL_SIZE)])
def test_ota_metadata_with_verify_certificate(
    tmp_path: Path,
    dir_test: Path,
    payload_str: str,
):
    certs_dir = tmp_path / "certs"
    certs_dir.mkdir()
    cert_a_1 = certs_dir / "a.1.pem"
    cert_a_2 = certs_dir / "a.2.pem"
    cert_b_1 = certs_dir / "b.1.pem"
    cert_b_2 = certs_dir / "b.2.pem"

    # a.1.pem and a.2.pem is illegal
    cert_a_1.write_bytes(Path(dir_test / "keys" / "sign.pem").read_bytes())
    cert_a_2.write_bytes(Path(dir_test / "keys" / "sign.pem").read_bytes())
    # b.1.pem and b.2.pem isillegal
    cert_b_1.write_bytes(Path(dir_test / "keys" / "root.pem").read_bytes())
    cert_b_2.write_bytes(Path(dir_test / "keys" / "interm.pem").read_bytes())

    metadata_jwt = generate_jwt(payload_str, dir_test)
    parser = _MetadataJWTParser(metadata_jwt, certs_dir=str(certs_dir))
    metadata = parser.get_otametadata()
    assert metadata.directory.asdict() == DIR_INFO
    assert metadata.symboliclink.asdict() == SYMLINK_INFO
    assert metadata.regular.asdict() == REGULAR_INFO
    assert metadata.persistent.asdict() == PERSISTENT_INFO
    assert metadata.certificate.asdict() == CERTIFICATE_INFO
    assert metadata.rootfs_directory == ROOTFS_DIR_INFO
    parser.verify_metadata(Path(dir_test / "keys" / "sign.pem").read_bytes())
    if "total_regular_size" in payload_str:
        assert metadata.total_regular_size == TOTAL_REGULAR_SIZE
    else:
        assert metadata.total_regular_size is None


@pytest.mark.parametrize("payload_str", [(PAYLOAD), (PAYLOAD_W_TOTAL_SIZE)])
def test_ota_metadata_with_verify_certificate_exception(
    dir_test: Path,
    tmp_path: Path,
    payload_str,
):
    certs_dir = tmp_path / "certs"
    certs_dir.mkdir()
    cert_a_1 = certs_dir / "a.1.pem"
    cert_a_2 = certs_dir / "a.2.pem"

    # a.1.pem and a.2.pem is illegal
    cert_a_1.write_bytes(Path(dir_test / "keys" / "sign.pem").read_bytes())
    cert_a_2.write_bytes(Path(dir_test / "keys" / "sign.pem").read_bytes())

    metadata_jwt = generate_jwt(payload_str, dir_test)
    parser = _MetadataJWTParser(metadata_jwt, certs_dir=str(certs_dir))
    metadata = parser.get_otametadata()
    assert metadata.directory.asdict() == DIR_INFO
    assert metadata.symboliclink.asdict() == SYMLINK_INFO
    assert metadata.regular.asdict() == REGULAR_INFO
    assert metadata.persistent.asdict() == PERSISTENT_INFO
    assert metadata.certificate.asdict() == CERTIFICATE_INFO
    assert metadata.rootfs_directory == ROOTFS_DIR_INFO
    with pytest.raises(ValueError):
        # NOTE: intentionally use sign.key as sign cert here,
        #       to test verify against invalid cert
        parser.verify_metadata(Path(dir_test / "keys" / "sign.key").read_bytes())
    if "total_regular_size" in payload_str:
        assert metadata.total_regular_size == TOTAL_REGULAR_SIZE
    else:
        assert metadata.total_regular_size is None


# ------ text based ota metafiles parsing test ------ #

# try to include as any special characters as possible
@pytest.mark.parametrize(
    "_input,mode,uid, gid,nlink,  _hash,  path,  size, inode, compression_alg",
    (
        # rev4: mode,uid,gid,link number,sha256sum,'path/to/file'[,size[,inode,[compression_alg]]]
        (
            r"0644,1000,1000,3,0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef,'/aaa\,'\'',233/to/file',1234,12345678,zst",
            int("0644", 8),
            1000,
            1000,
            3,
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            r"/aaa\,',233/to/file",
            1234,
            12345678,
            "zst",
        ),
        (
            r"0644,1000,1000,3,0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef,'/aaa\,'\'',233/to/file',1234,12345678,",
            int("0644", 8),
            1000,
            1000,
            3,
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            r"/aaa\,',233/to/file",
            1234,
            12345678,
            "",
        ),
        (
            r"0644,1000,1000,3,0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef,'/aaa\,'\'',233/to/file',1234,,zst",
            int("0644", 8),
            1000,
            1000,
            3,
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            r"/aaa\,',233/to/file",
            1234,
            0,
            "zst",
        ),
        # rev3: mode,uid,gid,link number,sha256sum,'path/to/file'[,size[,inode]]
        (
            r"0644,1000,1000,3,0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef,'/aaa\,'\'',233/to/file',1234,12345678",
            int("0644", 8),
            1000,
            1000,
            3,
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            r"/aaa\,',233/to/file",
            1234,
            12345678,
            "",  # (new in rev4)
        ),
        (
            r"0644,1000,1000,3,0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef,'/aaa\,'\'',233/to/file',1234,",
            int("0644", 8),
            1000,
            1000,
            3,
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            r"/aaa\,',233/to/file",
            1234,
            0,
            "",  # (new in rev4)
        ),
        (
            r"0644,1000,1000,3,0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef,'/aaa\,'\'',233/to/file',,",
            int("0644", 8),
            1000,
            1000,
            3,
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            r"/aaa\,',233/to/file",
            0,
            0,
            "",  # (new in rev4)
        ),
        # rev2: mode,uid,gid,link number,sha256sum,'path/to/file'[,size]
        (
            r"0644,1000,1000,1,0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef,'/aaa,'\'',233/to/file',1234",
            int("0644", 8),
            1000,
            1000,
            1,
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            r"/aaa,',233/to/file",
            1234,
            0,  # (new in rev3)
            "",  # (new in rev4)
        ),
        # rev1: mode,uid,gid,link number,sha256sum,'path/to/file'
        (
            r"0644,1000,1000,1,0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef,'/aaa,'\'',233/to/file'",
            int("0644", 8),
            1000,
            1000,
            1,
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            r"/aaa,',233/to/file",
            0,  # (new in rev2)
            0,  # (new in rev3)
            "",  # (new in rev4)
        ),
    ),
)
def test_RegularInf(
    _input: str,
    mode: int,
    uid: int,
    gid: int,
    nlink: int,
    _hash: str,
    path: str,
    size: int,
    inode: int,
    compression_alg: str,
):
    entry = parse_regulars_from_txt(_input)
    assert entry.mode == mode
    assert entry.uid == uid
    assert entry.gid == gid
    assert entry.nlink == nlink
    assert entry.sha256hash == bytes.fromhex(_hash)
    assert entry.path == path
    assert entry.size == size
    assert entry.inode == inode
    assert entry.compressed_alg == compression_alg


@pytest.mark.parametrize(
    "_input, mode, uid, gid, path",
    (
        (
            r"0755,0,0,'/usr/lib/python3/aaa,'\''bbb'",
            int("0755", 8),
            0,
            0,
            r"/usr/lib/python3/aaa,'bbb",
        ),
    ),
)
def test_DirectoryInf(_input: str, mode: int, uid: int, gid: int, path: str):
    entry = parse_dirs_from_txt(_input)

    assert entry.mode == mode
    assert entry.uid == uid
    assert entry.gid == gid
    assert entry.path == path


@pytest.mark.parametrize(
    "_input, mode, uid, gid, link, target",
    (
        # ensure ' are escaped and (,') will not break path parsing
        (
            r"0777,0,0,'/var/lib/ieee-data/iab.csv','../../ieee-data/'\'','\''iab.csv'",
            int("0777", 8),
            0,
            0,
            r"/var/lib/ieee-data/iab.csv",
            r"../../ieee-data/','iab.csv",
        ),
    ),
)
def test_SymbolicLinkInf(
    _input: str, mode: int, uid: int, gid: int, link: str, target: str
):
    entry = parse_symlinks_from_txt(_input)

    assert entry.mode == mode
    assert entry.uid == uid
    assert entry.gid == gid
    assert entry.slink == link
    assert entry.srcpath == target


@pytest.mark.parametrize(
    "_input, path",
    (
        (
            r"'/etc/net'\''plan'",
            r"/etc/net'plan",
        ),
    ),
)
def test_PersistentInf(_input: str, path: str):
    entry = parse_persistents_from_txt(_input)
    assert entry.path == path
