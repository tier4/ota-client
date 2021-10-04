#!/usr/bin/env python3

import re
import pytest
import base64
from pathlib import Path
from OpenSSL import crypto
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

test_dir = Path(__file__).parent

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
ROOTFS_DIR_INFO = {"file": ROOTFS_DIR}

CERTIFICATE_FNAME = "ota-intermediate.pem"
CERTIFICATE_HASH = "24c0c9ea292458398f05b9b2a31b483c45d27e284743a3f0c7963e2ac0c62ed2"
CERTIFICATE_INFO = {"file": CERTIFICATE_FNAME, "hash": CERTIFICATE_HASH}


def sign(sign_key_file, data):
    with open(sign_key_file, "rb") as f:
        priv = serialization.load_pem_private_key(f.read(), password=None)
    return urlsafe_b64encode(priv.sign(data.encode(), ec.ECDSA(hashes.SHA256())))


def urlsafe_b64encode(data):
    if type(data) is str:
        data = data.encode()
    return base64.urlsafe_b64encode(data).decode()


@pytest.fixture
def generate_jwt():
    sign_key_file = test_dir / "keys" / "sign.key"

    header = urlsafe_b64encode(HEADER)
    payload = urlsafe_b64encode(PAYLOAD)
    signature = sign(sign_key_file, f"{header}.{payload}")
    return f"{header}.{payload}.{signature}"


def test_ota_metadata(generate_jwt):
    from ota_metadata import OtaMetadata

    metadata = OtaMetadata(generate_jwt)
    assert metadata.get_directories_info() == DIR_INFO
    assert metadata.get_symboliclinks_info() == SYMLINK_INFO
    assert metadata.get_regulars_info() == REGULAR_INFO
    assert metadata.get_persistent_info() == PERSISTENT_INFO
    assert metadata.get_rootfsdir_info() == ROOTFS_DIR_INFO
    assert metadata.get_certificate_info() == CERTIFICATE_INFO
    metadata.verify(open(test_dir / "keys" / "sign.pem").read())


def test_ota_metadata_exception(generate_jwt):
    from ota_metadata import OtaMetadata
    from ota_error import OtaErrorUnrecoverable, OtaErrorRecoverable

    metadata = OtaMetadata(generate_jwt)
    with pytest.raises(OtaErrorRecoverable):
        # sing.key is invalid pem
        metadata.verify(open(test_dir / "keys" / "sign.key").read())


def test_ota_metadata_with_verify_certificate(mocker, generate_jwt, tmp_path):
    from ota_metadata import OtaMetadata

    certs_dir = tmp_path / "certs"
    certs_dir.mkdir()
    cert_a_1 = certs_dir / "a.1.pem"
    cert_a_2 = certs_dir / "a.2.pem"
    cert_b_1 = certs_dir / "b.1.pem"
    cert_b_2 = certs_dir / "b.2.pem"

    # a.1.pem and a.2.pem is illegal
    cert_a_1.write_bytes(open(test_dir / "keys" / "sign.pem", "rb").read())
    cert_a_2.write_bytes(open(test_dir / "keys" / "sign.pem", "rb").read())
    # b.1.pem and b.2.pem isillegal
    cert_b_1.write_bytes(open(test_dir / "keys" / "root.pem", "rb").read())
    cert_b_2.write_bytes(open(test_dir / "keys" / "interm.pem", "rb").read())

    mocker.patch.object(OtaMetadata, "CERTS_DIR", certs_dir)

    metadata = OtaMetadata(generate_jwt)
    assert metadata.get_directories_info() == DIR_INFO
    assert metadata.get_symboliclinks_info() == SYMLINK_INFO
    assert metadata.get_regulars_info() == REGULAR_INFO
    assert metadata.get_persistent_info() == PERSISTENT_INFO
    assert metadata.get_rootfsdir_info() == ROOTFS_DIR_INFO
    assert metadata.get_certificate_info() == CERTIFICATE_INFO
    metadata.verify(open(test_dir / "keys" / "sign.pem").read())


def test_ota_metadata_with_verify_certificate_exception(mocker, generate_jwt, tmp_path):
    from ota_metadata import OtaMetadata
    from ota_error import OtaErrorUnrecoverable, OtaErrorRecoverable

    certs_dir = tmp_path / "certs"
    certs_dir.mkdir()
    cert_a_1 = certs_dir / "a.1.pem"
    cert_a_2 = certs_dir / "a.2.pem"

    # a.1.pem and a.2.pem is illegal
    cert_a_1.write_bytes(open(test_dir / "keys" / "sign.pem", "rb").read())
    cert_a_2.write_bytes(open(test_dir / "keys" / "sign.pem", "rb").read())

    mocker.patch.object(OtaMetadata, "CERTS_DIR", certs_dir)

    metadata = OtaMetadata(generate_jwt)
    assert metadata.get_directories_info() == DIR_INFO
    assert metadata.get_symboliclinks_info() == SYMLINK_INFO
    assert metadata.get_regulars_info() == REGULAR_INFO
    assert metadata.get_persistent_info() == PERSISTENT_INFO
    assert metadata.get_rootfsdir_info() == ROOTFS_DIR_INFO
    assert metadata.get_certificate_info() == CERTIFICATE_INFO
    with pytest.raises(OtaErrorRecoverable):
        metadata.verify(open(test_dir / "keys" / "sign.pem").read())
