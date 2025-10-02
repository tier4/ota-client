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
"""OTA image metadata.jwt parser implementation.

OTA metadata format definition: https://tier4.atlassian.net/l/cp/PCvwC6qk

Version1 JWT verification algorithm: ES256
Version1 JWT payload layout(revision2):
[
    {"version": 1}, # must
    {"directory": "dirs.txt", "hash": <sha256_hash>}, # must
    {"symboliclink": "symlinks.txt", "hash": <sha256_hash>}, # must
    {"regular": "regulars.txt", "hash": <sha256_hash>}, # must
    {"persistent": "persistents.txt", "hash": <sha256_hash>}, # must
    {"certificate": "sign.pem", "hash": <sha256_hash>}, # must
    {"rootfs_directory": "data"}, # must
    {"total_regular_size": "23637537004"}, # revision1: optional
    {"compressed_rootfs_directory": "data.zstd"} # revision2: optional
]
Version1 OTA metafiles list:
- directory: all directories in the image,
- symboliclink: all symlinks in the image,
- regular: all normal/regular files in the image,
- persistent: files that should be preserved across update.

"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, ClassVar

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ec import ECDSA, EllipticCurvePublicKey
from cryptography.x509 import load_pem_x509_certificate
from pydantic import BaseModel
from typing_extensions import Self

from ota_metadata.errors import (
    ImageMetadataInvalid,
    NoCAStoreAvailable,
    SignCertInvalid,
)
from ota_metadata.utils.cert_store import CAChainStore
from otaclient_common._typing import StrEnum

logger = logging.getLogger(__name__)


class MetafilesV1(StrEnum):
    """version1 text based ota metafiles fname.
    Check _MetadataJWTClaimsLayout for more details.
    """

    regular = "regular"
    directory = "directory"
    symboliclink = "symboliclink"
    persistent = "persistent"
    certificate = "certificate"

    @classmethod
    def check_field_name_is_member(cls, _in: Any) -> bool:
        return _in in cls.__members__


class MetaFile(BaseModel):
    file: str
    hash: str


class MetadataJWTParser:
    """Implementation of custom JWT parsing/loading/validating logic.

    Certification based JWT verification:
    - Verification algorithm: ES256.
    - certification fname is in "certificate" entry in JWT.
    - The root and intermediate certificates exist under certs_dir.
        When A.root.pem, A.intermediate.pem, B.root.pem, and B.intermediate.pem
        exist, the groups of A* and the groups of B* are handled as a chained
        certificate.
        verify function verifies specified certificate with them.
        Certificates file name format should be: '.*\\..*.pem'

    - NOTE:
        If there is no root or intermediate certificate, certification verification
        will be skipped! This SHOULD ONLY happen at non-production environment!
    """

    ES256 = ECDSA(algorithm=hashes.SHA256())

    def __init__(self, metadata_jwt: str, *, ca_chains_store: CAChainStore):
        self.ca_chains_store = ca_chains_store

        # pre_parse metadata_jwt
        jwt_list = metadata_jwt.split(".")
        # the content(b64encoded, bytes) of metadata
        self.metadata_bytes = ".".join(jwt_list[:2]).encode()
        # the signature of content
        self.metadata_signature = base64.urlsafe_b64decode(jwt_list[2])
        # header(bytes) and payload(bytes) parts of metadata content
        self.header_bytes = base64.urlsafe_b64decode(jwt_list[0])
        self.payload_bytes = base64.urlsafe_b64decode(jwt_list[1])

        # parse metadata payload into OTAMetadata
        self._metadata_jwt = MetadataJWTClaimsLayout.parse_payload(self.payload_bytes)

    @property
    def metadata_jwt(self) -> MetadataJWTClaimsLayout:
        return self._metadata_jwt

    def verify_metadata_cert(self, metadata_cert: bytes) -> None:
        """Verify the metadata's sign certificate against local pinned CA.

        Raises:
            Raise MetadataJWTVerificationFailed on verification failed.
        """
        if not self.ca_chains_store:
            _err_msg = "CA chains store is empty!!! immediately fail the verification"
            logger.error(_err_msg)
            raise NoCAStoreAvailable(_err_msg)

        try:
            cert_to_verify = load_pem_x509_certificate(metadata_cert)
        except Exception as e:
            _err_msg = f"invalid certificate {metadata_cert}: {e!r}"
            logger.exception(_err_msg, exc_info=e)
            raise SignCertInvalid(_err_msg) from e

        hit_cachain = self.ca_chains_store.verify(cert_to_verify)
        if hit_cachain:
            return

        _err_msg = f"metadata sign certificate {metadata_cert} could not be verified"
        logger.error(_err_msg)
        raise SignCertInvalid(_err_msg)

    def verify_metadata_signature(self, metadata_cert: bytes):
        """Verify metadata against sign certificate.

        Raises:
            Raise MetadataJWTVerificationFailed on validation failed.
        """
        try:
            cert = load_pem_x509_certificate(metadata_cert)
            logger.debug(f"verify data: {self.metadata_bytes=}")
            _pubkey: EllipticCurvePublicKey = cert.public_key()  # type: ignore[assignment]

            _pubkey.verify(
                signature=self.metadata_signature,
                data=self.metadata_bytes,
                signature_algorithm=self.ES256,
            )
        except Exception as e:
            msg = f"failed to verify metadata against sign cert: {e!r}"
            logger.error(msg, exc_info=e)
            raise ImageMetadataInvalid(msg) from e


class MetadataJWTClaimsLayout(BaseModel):
    """Version1 metadata.jwt payload mapping and parsing."""

    SCHEME_VERSION: ClassVar[int] = 1
    VERSION_KEY: ClassVar[str] = "version"

    # metadata scheme
    version: int

    total_regular_size: int = 0
    rootfs_directory: str
    compressed_rootfs_directory: str = ""

    # sign certificate
    certificate: MetaFile

    # metadata files definition
    directory: MetaFile
    symboliclink: MetaFile
    regular: MetaFile
    persistent: MetaFile

    @classmethod
    def parse_payload(cls, payload: str | bytes) -> Self:
        """
        NOTE: in version1, payload is a list of dict,
          check module docstring for more details.

        Raises:
            ImageMetadataInvalid on invalide metadata.jwt.
        """
        try:
            claims: list[dict[str, Any]] = json.loads(payload)
        except Exception as e:
            _err_msg = "metadata.jwt doesn't contain valid json payload"
            logger.error(_err_msg, exc_info=e)
            raise ImageMetadataInvalid(_err_msg) from e

        if not claims or not isinstance(claims, list):
            _err_msg = f"metadata.jwt v1 is a list of JSON object, invalid {payload=}"
            logger.error(_err_msg)
            raise ImageMetadataInvalid(_err_msg)

        _res_dict = {}
        for field_dict in claims:
            for k, v in field_dict.items():
                if MetafilesV1.check_field_name_is_member(k):
                    _res_dict[k] = MetaFile(
                        file=v,
                        hash=field_dict["hash"],
                    )
                    break

                _res_dict[k] = v
                if k == cls.VERSION_KEY and str(v) != str(cls.SCHEME_VERSION):
                    raise ImageMetadataInvalid(f"expecting version 1, get {v}")

        try:
            return cls.model_validate(_res_dict)
        except Exception as e:
            _err_msg = f"metadata.jwt payload validation failed: {e!r}"
            logger.error(_err_msg, exc_info=e)
            raise ImageMetadataInvalid(_err_msg) from e
