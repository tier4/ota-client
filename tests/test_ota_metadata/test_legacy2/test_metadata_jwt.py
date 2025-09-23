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
"""
NOTE: basically copied from test_legacy.py
"""


from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ec import ECDSA
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509 import (
    BasicConstraints,
    CertificateBuilder,
    KeyUsage,
    Name,
    NameAttribute,
    load_pem_x509_certificate,
)
from cryptography.x509.oid import NameOID

from ota_metadata.legacy2.metadata_jwt import (
    MetadataJWTClaimsLayout,
    MetadataJWTParser,
    MetaFile,
)
from ota_metadata.utils.cert_store import CAChain, CAChainStore

# --- valid metadata.jwt --- #
# original layout
PAYLOAD = """\
[\
{"version": 1}, \
{"directory": "dirs.txt", "hash": "43afbd19eab7c9e27f402a3332c38d072a69c7932fb35c32c1fc7069695235f1"}, \
{"symboliclink": "symlinks.txt", "hash": "6643bf896d3ac3bd4034d742fae0d7eb82bd384062492235404114aeb34efd7d"}, \
{"regular": "regulars.txt", "hash": "a390f92fe49b8402a2bb9f0b594e9de70f70dc6bf429031ac4d0b21365251600"}, \
{"persistent": "persistents.txt", "hash": "3195ded730474d0181257204ba0fd79766721ab62ace395f20decd44983cb2d3"}, \
{"rootfs_directory": "data"}, \
{"certificate": "certificate.pem", "hash": "24c0c9ea292458398f05b9b2a31b483c45d27e284743a3f0c7963e2ac0c62ed2"}\
]\
"""
PAYLOAD_PARSED = MetadataJWTClaimsLayout(
    version=1,
    directory=MetaFile(
        file="dirs.txt",
        hash="43afbd19eab7c9e27f402a3332c38d072a69c7932fb35c32c1fc7069695235f1",
    ),
    symboliclink=MetaFile(
        file="symlinks.txt",
        hash="6643bf896d3ac3bd4034d742fae0d7eb82bd384062492235404114aeb34efd7d",
    ),
    regular=MetaFile(
        file="regulars.txt",
        hash="a390f92fe49b8402a2bb9f0b594e9de70f70dc6bf429031ac4d0b21365251600",
    ),
    persistent=MetaFile(
        file="persistents.txt",
        hash="3195ded730474d0181257204ba0fd79766721ab62ace395f20decd44983cb2d3",
    ),
    rootfs_directory="data",
    certificate=MetaFile(
        file="certificate.pem",
        hash="24c0c9ea292458398f05b9b2a31b483c45d27e284743a3f0c7963e2ac0c62ed2",
    ),
)

# revision1: add total_regular_size
PAYLOAD_W_TOTAL_SIZE = """\
[\
{"version": 1}, \
{"directory": "dirs.txt", "hash": "43afbd19eab7c9e27f402a3332c38d072a69c7932fb35c32c1fc7069695235f1"}, \
{"symboliclink": "symlinks.txt", "hash": "6643bf896d3ac3bd4034d742fae0d7eb82bd384062492235404114aeb34efd7d"}, \
{"regular": "regulars.txt", "hash": "a390f92fe49b8402a2bb9f0b594e9de70f70dc6bf429031ac4d0b21365251600"}, \
{"persistent": "persistents.txt", "hash": "3195ded730474d0181257204ba0fd79766721ab62ace395f20decd44983cb2d3"}, \
{"rootfs_directory": "data"}, \
{"certificate": "certificate.pem", "hash": "24c0c9ea292458398f05b9b2a31b483c45d27e284743a3f0c7963e2ac0c62ed2"}, \
{"total_regular_size": "108708870"}\
]\
"""
PAYLOAD_W_TOTAL_SIZE_PARSED = MetadataJWTClaimsLayout(
    version=1,
    directory=MetaFile(
        file="dirs.txt",
        hash="43afbd19eab7c9e27f402a3332c38d072a69c7932fb35c32c1fc7069695235f1",
    ),
    symboliclink=MetaFile(
        file="symlinks.txt",
        hash="6643bf896d3ac3bd4034d742fae0d7eb82bd384062492235404114aeb34efd7d",
    ),
    regular=MetaFile(
        file="regulars.txt",
        hash="a390f92fe49b8402a2bb9f0b594e9de70f70dc6bf429031ac4d0b21365251600",
    ),
    persistent=MetaFile(
        file="persistents.txt",
        hash="3195ded730474d0181257204ba0fd79766721ab62ace395f20decd44983cb2d3",
    ),
    rootfs_directory="data",
    certificate=MetaFile(
        file="certificate.pem",
        hash="24c0c9ea292458398f05b9b2a31b483c45d27e284743a3f0c7963e2ac0c62ed2",
    ),
    total_regular_size=108708870,
)

# revision2: add compressed_rootfs
PAYLOAD_W_COMPRESSED_ROOTFS = """\
[\
{"version": 1}, \
{"directory": "dirs.txt", "hash": "43afbd19eab7c9e27f402a3332c38d072a69c7932fb35c32c1fc7069695235f1"}, \
{"symboliclink": "symlinks.txt", "hash": "6643bf896d3ac3bd4034d742fae0d7eb82bd384062492235404114aeb34efd7d"}, \
{"regular": "regulars.txt", "hash": "a390f92fe49b8402a2bb9f0b594e9de70f70dc6bf429031ac4d0b21365251600"}, \
{"persistent": "persistents.txt", "hash": "3195ded730474d0181257204ba0fd79766721ab62ace395f20decd44983cb2d3"}, \
{"rootfs_directory": "data"}, \
{"certificate": "certificate.pem", "hash": "24c0c9ea292458398f05b9b2a31b483c45d27e284743a3f0c7963e2ac0c62ed2"}, \
{"total_regular_size": "108708870"},\
{"compressed_rootfs_directory": "data.zstd"}\
]\
"""
PAYLOAD_W_COMPRESSED_ROOTFS_PARSED = MetadataJWTClaimsLayout(
    version=1,
    directory=MetaFile(
        file="dirs.txt",
        hash="43afbd19eab7c9e27f402a3332c38d072a69c7932fb35c32c1fc7069695235f1",
    ),
    symboliclink=MetaFile(
        file="symlinks.txt",
        hash="6643bf896d3ac3bd4034d742fae0d7eb82bd384062492235404114aeb34efd7d",
    ),
    regular=MetaFile(
        file="regulars.txt",
        hash="a390f92fe49b8402a2bb9f0b594e9de70f70dc6bf429031ac4d0b21365251600",
    ),
    persistent=MetaFile(
        file="persistents.txt",
        hash="3195ded730474d0181257204ba0fd79766721ab62ace395f20decd44983cb2d3",
    ),
    rootfs_directory="data",
    certificate=MetaFile(
        file="certificate.pem",
        hash="24c0c9ea292458398f05b9b2a31b483c45d27e284743a3f0c7963e2ac0c62ed2",
    ),
    total_regular_size=108708870,
    compressed_rootfs_directory="data.zstd",
)

# --- invalid metadata.jwt --- #
PAYLOAD_MISSING_MUST_SET_FIELD_DIRECTORY = """\
[\
{"version": 1}, \
{"symboliclink": "symlinks.txt", "hash": "6643bf896d3ac3bd4034d742fae0d7eb82bd384062492235404114aeb34efd7d"}, \
{"regular": "regulars.txt", "hash": "a390f92fe49b8402a2bb9f0b594e9de70f70dc6bf429031ac4d0b21365251600"}, \
{"persistent": "persistents.txt", "hash": "3195ded730474d0181257204ba0fd79766721ab62ace395f20decd44983cb2d3"}, \
{"rootfs_directory": "data"}, \
{"certificate": "ota-intermediate.pem", "hash": "24c0c9ea292458398f05b9b2a31b483c45d27e284743a3f0c7963e2ac0c62ed2"}, \
{"total_regular_size": "108708870"},\
{"compressed_rootfs_directory": "data.zstd"}\
]\
"""

PAYLOAD_MISSING_MUST_SET_FIELD_ROOTFS = """\
[\
{"version": 1}, \
{"directory": "dirs.txt", "hash": "43afbd19eab7c9e27f402a3332c38d072a69c7932fb35c32c1fc7069695235f1"}, \
{"symboliclink": "symlinks.txt", "hash": "6643bf896d3ac3bd4034d742fae0d7eb82bd384062492235404114aeb34efd7d"}, \
{"regular": "regulars.txt", "hash": "a390f92fe49b8402a2bb9f0b594e9de70f70dc6bf429031ac4d0b21365251600"}, \
{"persistent": "persistents.txt", "hash": "3195ded730474d0181257204ba0fd79766721ab62ace395f20decd44983cb2d3"}, \
{"certificate": "ota-intermediate.pem", "hash": "24c0c9ea292458398f05b9b2a31b483c45d27e284743a3f0c7963e2ac0c62ed2"}, \
{"total_regular_size": "108708870"},\
{"compressed_rootfs_directory": "data.zstd"}\
]\
"""


@pytest.mark.parametrize(
    "_input, _expected",
    (
        (PAYLOAD, PAYLOAD_PARSED),
        (PAYLOAD_W_TOTAL_SIZE, PAYLOAD_W_TOTAL_SIZE_PARSED),
        (PAYLOAD_W_COMPRESSED_ROOTFS, PAYLOAD_W_COMPRESSED_ROOTFS_PARSED),
        # invalid metadata.jwt
        (PAYLOAD_MISSING_MUST_SET_FIELD_DIRECTORY, None),
        (PAYLOAD_MISSING_MUST_SET_FIELD_ROOTFS, None),
    ),
)
def test_parse_raw_jwt(_input, _expected) -> None:
    try:
        assert MetadataJWTClaimsLayout.parse_payload(_input) == _expected
    except Exception as e:
        assert _expected is None, f"expecting {_input} is valid, but: {e!r}"


class TestCryptographyCompatibility:
    """Compatibility tests for cryptography library"""

    def test_ecdsa_signature_verification_basic(self):
        """Basic ECDSA signature verification test"""
        # Generate test private key
        private_key = ec.generate_private_key(ec.SECP256R1())
        public_key = private_key.public_key()

        # Test data
        test_data = b"test message for signature verification"

        # Generate signature
        signature = private_key.sign(test_data, ECDSA(hashes.SHA256()))

        # Verify signature (valid case)
        public_key.verify(signature, test_data, ECDSA(hashes.SHA256()))

        # Signature verification (invalid case - different data)
        from cryptography.exceptions import InvalidSignature

        with pytest.raises(InvalidSignature):
            public_key.verify(signature, b"different data", ECDSA(hashes.SHA256()))

    def test_x509_certificate_loading_and_parsing(self):
        """X.509 certificate loading and parsing test"""
        # Generate test self-signed certificate
        private_key = ec.generate_private_key(ec.SECP256R1())

        subject = issuer = Name(
            [
                NameAttribute(NameOID.COUNTRY_NAME, "JP"),
                NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "Tokyo"),
                NameAttribute(NameOID.LOCALITY_NAME, "Tokyo"),
                NameAttribute(NameOID.ORGANIZATION_NAME, "Test Organization"),
                NameAttribute(NameOID.COMMON_NAME, "test.example.com"),
            ]
        )

        cert = (
            CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(private_key.public_key())
            .serial_number(1)
            .not_valid_before(datetime.now(timezone.utc))
            .not_valid_after(datetime(2030, 1, 1, tzinfo=timezone.utc))
            .add_extension(
                BasicConstraints(ca=True, path_length=None),
                critical=True,
            )
            .sign(private_key, hashes.SHA256())
        )

        # Encode to PEM format
        cert_pem = cert.public_bytes(Encoding.PEM)

        # Load PEM format certificate
        loaded_cert = load_pem_x509_certificate(cert_pem)

        # Verify certificate contents
        assert loaded_cert.subject == subject
        assert loaded_cert.issuer == issuer
        assert loaded_cert.serial_number == 1

        # Public key verification
        loaded_public_key = loaded_cert.public_key()

        # Test if public keys are the same (verify with signature)
        test_data = b"test data for public key verification"
        signature = private_key.sign(test_data, ECDSA(hashes.SHA256()))
        # Cast to EllipticCurvePublicKey for type checking
        from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicKey

        assert isinstance(loaded_public_key, EllipticCurvePublicKey)
        loaded_public_key.verify(signature, test_data, ECDSA(hashes.SHA256()))

    def test_certificate_chain_verification(self):
        """Certificate chain verification test"""
        # Generate root certificate
        root_key = ec.generate_private_key(ec.SECP256R1())
        root_subject = Name(
            [
                NameAttribute(NameOID.COMMON_NAME, "Test Root CA"),
                NameAttribute(NameOID.ORGANIZATION_NAME, "Test Organization"),
            ]
        )

        root_cert = (
            CertificateBuilder()
            .subject_name(root_subject)
            .issuer_name(root_subject)  # Self-signed
            .public_key(root_key.public_key())
            .serial_number(1)
            .not_valid_before(datetime.now(timezone.utc))
            .not_valid_after(datetime(2030, 1, 1, tzinfo=timezone.utc))
            .add_extension(
                BasicConstraints(ca=True, path_length=1),
                critical=True,
            )
            .add_extension(
                KeyUsage(
                    digital_signature=True,
                    key_cert_sign=True,
                    crl_sign=True,
                    key_agreement=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    content_commitment=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .sign(root_key, hashes.SHA256())
        )

        # Generate intermediate certificate
        intermediate_key = ec.generate_private_key(ec.SECP256R1())
        intermediate_subject = Name(
            [
                NameAttribute(NameOID.COMMON_NAME, "Test Intermediate CA"),
                NameAttribute(NameOID.ORGANIZATION_NAME, "Test Organization"),
            ]
        )

        intermediate_cert = (
            CertificateBuilder()
            .subject_name(intermediate_subject)
            .issuer_name(root_subject)
            .public_key(intermediate_key.public_key())
            .serial_number(2)
            .not_valid_before(datetime.now(timezone.utc))
            .not_valid_after(datetime(2030, 1, 1, tzinfo=timezone.utc))
            .add_extension(
                BasicConstraints(ca=True, path_length=0),
                critical=True,
            )
            .add_extension(
                KeyUsage(
                    digital_signature=True,
                    key_cert_sign=True,
                    crl_sign=True,
                    key_agreement=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    content_commitment=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .sign(root_key, hashes.SHA256())  # Signed by root certificate
        )

        # Generate end entity certificate
        end_key = ec.generate_private_key(ec.SECP256R1())
        end_subject = Name(
            [
                NameAttribute(NameOID.COMMON_NAME, "Test End Entity"),
                NameAttribute(NameOID.ORGANIZATION_NAME, "Test Organization"),
            ]
        )

        end_cert = (
            CertificateBuilder()
            .subject_name(end_subject)
            .issuer_name(intermediate_subject)
            .public_key(end_key.public_key())
            .serial_number(3)
            .not_valid_before(datetime.now(timezone.utc))
            .not_valid_after(datetime(2030, 1, 1, tzinfo=timezone.utc))
            .add_extension(
                BasicConstraints(ca=False, path_length=None),
                critical=True,
            )
            .add_extension(
                KeyUsage(
                    digital_signature=True,
                    key_cert_sign=False,
                    crl_sign=False,
                    key_agreement=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    content_commitment=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .sign(
                intermediate_key, hashes.SHA256()
            )  # Signed by intermediate certificate
        )

        # Create and test CAChain
        ca_chain = CAChain()
        ca_chain.add_cert(root_cert)
        ca_chain.add_cert(intermediate_cert)

        # Internal check
        ca_chain.internal_check()  # Should have root certificate

        # Verify end entity certificate
        ca_chain.verify(end_cert)  # Should verify successfully

    def test_certificate_validity_period_check(self):
        """Certificate validity period check test"""
        # Past certificate (expired)
        expired_key = ec.generate_private_key(ec.SECP256R1())
        expired_subject = Name(
            [
                NameAttribute(NameOID.COMMON_NAME, "Expired Certificate"),
            ]
        )

        expired_cert = (
            CertificateBuilder()
            .subject_name(expired_subject)
            .issuer_name(expired_subject)
            .public_key(expired_key.public_key())
            .serial_number(1)
            .not_valid_before(datetime(2020, 1, 1, tzinfo=timezone.utc))
            .not_valid_after(datetime(2021, 1, 1, tzinfo=timezone.utc))  # Expired
            .sign(expired_key, hashes.SHA256())
        )

        # Expired certificate verification should fail
        ca_chain = CAChain()
        ca_chain.add_cert(expired_cert)

        with pytest.raises(ValueError, match="cert is not within valid period"):
            ca_chain.verify(expired_cert)

    def test_jwt_signature_flow_integration(self):
        """JWT signature flow integration test"""
        # Prepare test data
        private_key = ec.generate_private_key(ec.SECP256R1())

        # JWT header
        header = {"alg": "ES256", "typ": "JWT"}
        header_encoded = base64.urlsafe_b64encode(
            json.dumps(header, separators=(",", ":")).encode()
        ).decode()

        # JWT payload (test metadata)
        payload = [
            {"version": 1},
            {"directory": "dirs.txt", "hash": "test_hash_directory"},
            {"symboliclink": "symlinks.txt", "hash": "test_hash_symlinks"},
            {"regular": "regulars.txt", "hash": "test_hash_regulars"},
            {"persistent": "persistents.txt", "hash": "test_hash_persistents"},
            {"rootfs_directory": "data"},
            {"certificate": "certificate.pem", "hash": "test_hash_certificate"},
        ]
        payload_encoded = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode()
        ).decode()

        # Data to be signed
        signing_input = f"{header_encoded}.{payload_encoded}".encode()

        # Generate signature
        signature = private_key.sign(signing_input, ECDSA(hashes.SHA256()))
        signature_encoded = base64.urlsafe_b64encode(signature).decode()

        # Assemble JWT
        jwt_token = f"{header_encoded}.{payload_encoded}.{signature_encoded}"

        # Create certificate (for signature verification)
        subject = Name(
            [
                NameAttribute(NameOID.COMMON_NAME, "Test Signing Certificate"),
            ]
        )

        cert = (
            CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(private_key.public_key())
            .serial_number(1)
            .not_valid_before(datetime.now(timezone.utc))
            .not_valid_after(datetime(2030, 1, 1, tzinfo=timezone.utc))
            .sign(private_key, hashes.SHA256())
        )

        cert_pem = cert.public_bytes(Encoding.PEM)

        # Prepare CAChainStore
        ca_chain = CAChain()
        ca_chain.set_chain_prefix("test")
        ca_chain.add_cert(cert)
        ca_chains_store = CAChainStore()
        ca_chains_store.add_chain(ca_chain)

        # Verification with MetadataJWTParser
        parser = MetadataJWTParser(jwt_token, ca_chains_store=ca_chains_store)

        # Verify metadata parsing
        metadata = parser.metadata_jwt
        assert metadata.version == 1
        assert metadata.rootfs_directory == "data"
        assert metadata.directory.file == "dirs.txt"
        assert metadata.directory.hash == "test_hash_directory"

        # Certificate verification
        parser.verify_metadata_cert(cert_pem)

        # Signature verification
        parser.verify_metadata_signature(cert_pem)

    def test_cryptography_version_compatibility(self):
        """Cryptography library version compatibility test"""
        import cryptography
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.ec import (
            ECDSA,
            EllipticCurvePublicKey,
        )
        from cryptography.x509 import load_pem_x509_certificate

        # Output version information (for debugging)
        print(f"cryptography version: {cryptography.__version__}")

        # Check if basic APIs are available
        assert hashes is not None
        assert ec is not None
        assert ECDSA is not None
        assert EllipticCurvePublicKey is not None
        assert load_pem_x509_certificate is not None

    def test_error_handling_with_invalid_certificates(self):
        """Error handling test for invalid certificates"""
        # Invalid PEM data
        invalid_pem = (
            b"-----BEGIN CERTIFICATE-----\nINVALID_DATA\n-----END CERTIFICATE-----"
        )

        with pytest.raises(ValueError):
            load_pem_x509_certificate(invalid_pem)

        # Empty data
        with pytest.raises(ValueError):
            load_pem_x509_certificate(b"")

        # Completely invalid data
        with pytest.raises(ValueError):
            load_pem_x509_certificate(b"this is not a certificate")

    def test_signature_verification_edge_cases(self):
        """Signature verification edge cases test"""
        private_key = ec.generate_private_key(ec.SECP256R1())
        public_key = private_key.public_key()

        # Empty data
        empty_data = b""
        signature = private_key.sign(empty_data, ECDSA(hashes.SHA256()))
        public_key.verify(signature, empty_data, ECDSA(hashes.SHA256()))

        # Very long data
        long_data = b"a" * 1000000  # 1MB
        signature = private_key.sign(long_data, ECDSA(hashes.SHA256()))
        public_key.verify(signature, long_data, ECDSA(hashes.SHA256()))

        # Binary data
        binary_data = bytes(range(256))
        signature = private_key.sign(binary_data, ECDSA(hashes.SHA256()))
        public_key.verify(signature, binary_data, ECDSA(hashes.SHA256()))
