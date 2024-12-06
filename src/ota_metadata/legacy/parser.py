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
"""OTA metadata version1 implementation.

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
import shutil
import sqlite3
from dataclasses import dataclass, fields
from pathlib import Path
from typing import (
    Any,
    ClassVar,
    Dict,
    Generator,
    Generic,
    Iterator,
    List,
    Optional,
    TypeVar,
    Union,
    overload,
)

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ec import ECDSA, EllipticCurvePublicKey
from cryptography.x509 import load_pem_x509_certificate
from typing_extensions import Self

from ota_metadata.file_table import FileSystemTableORM
from ota_metadata.utils.cert_store import CAChainStore
from otaclient_common.common import urljoin_ensure_base
from otaclient_common.typing import StrEnum, StrOrPath

from .csv_parser import (
    parse_dirs_from_csv_file,
    parse_regulars_from_csv_file,
    parse_symlinks_from_csv_file,
)
from .rs_table import ResourceTableORM

logger = logging.getLogger(__name__)


class OTAImageInvalid(Exception):
    """OTA image itself is incompleted or metadata is missing."""


class OTARequestsAuthTokenInvalid(Exception):
    """Hit 401 or 403 when downloading metadata."""


class MetadataJWTPayloadInvalid(Exception):
    """Raised when verification passed, but input metadata.jwt is invalid."""


class MetadataJWTVerificationFailed(Exception): ...


FV = TypeVar("FV")


class MetafilesV1(StrEnum):
    """version1 text based ota metafiles fname.
    Check _MetadataJWTClaimsLayout for more details.
    """

    # entry_name: regular
    REGULAR_FNAME = "regulars.txt"
    # entry_name: directory
    DIRECTORY_FNAME = "dirs.txt"
    # entry_name: symboliclink
    SYMBOLICLINK_FNAME = "symlinks.txt"
    # entry_name: persistent
    PERSISTENT_FNAME = "persistents.txt"


@dataclass
class MetaFile:
    file: str
    hash: str


class MetaFieldDescriptor(Generic[FV]):
    """Field descriptor for dataclass _MetadataJWTClaimsLayout.

    This descriptor takes one dict from parsed metadata.jwt, parses
        and assigns it to each field in _MetadataJWTClaimsLayout.
        Check __set__ method's docstring for more details.
    """

    HASH_KEY = "hash"  # version1

    def __init__(self, field_type: type, *, default=None) -> None:
        self.field_type = field_type
        # NOTE: if field not presented, set to None
        self.default = default

    @overload
    def __get__(self, obj: None, objtype: type) -> Self:
        """Descriptor accessed via class."""
        ...

    @overload
    def __get__(self, obj, objtype: type) -> FV:
        """Descriptor accessed via bound instance."""
        ...

    def __get__(self, obj, objtype=None) -> Union[FV, Self]:
        if obj and not isinstance(obj, type):
            return getattr(obj, self._attrn)  # access via instance
        return self  # access via class, return the descriptor

    def __set__(self, obj, value: Any) -> None:
        """
        Expecting input value like:
        - a dict with one key-value pair,
            { "version": 1 }
        or
        - a dict with 2 fields,
            {
                <metafile_name>: <metafile_file_name>,
                "hash": "faaea3ee13b4cc0e107ab2840f792d480636bec6b4d1ccd4a0a3ce8046b2c549"
            }
        """
        if isinstance(value, self.__class__):
            setattr(obj, self._attrn, self.default)
            return  # handle dataclass default value setting

        if isinstance(value, dict):
            # metafile field
            if self.field_type is MetaFile:
                try:
                    return setattr(
                        obj,
                        self._attrn,
                        # NOTE: do str conversion
                        MetaFile(
                            file=str(value[self.field_name]),
                            hash=str(value[self.HASH_KEY]),
                        ),
                    )
                except KeyError:
                    raise ValueError(
                        f"invalid metafile field {self.field_name}: {value}"
                    ) from None
            # normal key-value field
            else:
                try:
                    # use <field_type> to do default conversion before assignment
                    return setattr(
                        obj, self._attrn, self.field_type(value[self.field_name])
                    )
                except KeyError:
                    raise ValueError(
                        f"invalid metafile field {self.field_name}: {value}"
                    ) from None
        raise ValueError(f"attempt to assign invalid {value=} to {self.field_name=}")

    def __set_name__(self, owner: type, name: str):
        self.name = name
        self.field_name = name
        self._attrn = f"_{owner.__name__}_{name}"

    # API

    def check_is_target_claim(self, value: Any) -> bool:
        """Check whether the input claim dict is for this field."""
        if not isinstance(value, dict):
            return False
        return self.field_name in value


class _MetadataJWTParser:
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
        logger.info(
            f"JWT header: {self.header_bytes.decode()}\n"
            f"JWT payload: {self.payload_bytes.decode()}\n"
            f"JWT signature: {self.metadata_signature}"
        )

        # parse metadata payload into OTAMetadata
        self.metadata_jwt = _MetadataJWTClaimsLayout.parse_payload(self.payload_bytes)
        logger.info(f"metadata={self.metadata_jwt!r}")

    def verify_metadata_cert(self, metadata_cert: bytes) -> None:
        """Verify the metadata's sign certificate against local pinned CA.

        Raises:
            Raise MetadataJWTVerificationFailed on verification failed.
        """
        if not self.ca_chains_store:
            _err_msg = "CA chains store is empty!!! immediately fail the verification"
            logger.error(_err_msg)
            raise MetadataJWTVerificationFailed(_err_msg)

        try:
            cert_to_verify = load_pem_x509_certificate(metadata_cert)
        except Exception as e:
            _err_msg = f"invalid certificate {metadata_cert}: {e!r}"
            logger.exception(_err_msg)
            raise MetadataJWTVerificationFailed(_err_msg) from e

        hit_cachain = self.ca_chains_store.verify(cert_to_verify)
        if hit_cachain:
            return

        _err_msg = f"metadata sign certificate {metadata_cert} could not be verified"
        logger.error(_err_msg)
        raise MetadataJWTVerificationFailed(_err_msg)

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
            logger.error(msg)
            raise MetadataJWTVerificationFailed(msg) from e

    def get_metadata_jwt(self) -> _MetadataJWTClaimsLayout:
        """Get parsed OTAMetaData.

        Returns:
            A instance of OTAMetaData representing the parsed(but not yet verified) metadata.
        """
        return self.metadata_jwt


# place holder for unset must field in _MetadataJWTClaimsLayout
_MUST_SET_CLAIM = object()


@dataclass
class _MetadataJWTClaimsLayout:
    """Version1 metadata.jwt payload mapping and parsing."""

    SCHEME_VERSION: ClassVar[int] = 1
    VERSION_KEY: ClassVar[str] = "version"

    # metadata scheme
    version: MetaFieldDescriptor[int] = MetaFieldDescriptor(
        int, default=_MUST_SET_CLAIM
    )
    # WARNING: total_regular_size should be int, but it comes as str in metadata.jwt,
    #          so we disable type_check for it, and convert it as field_type before assigning
    total_regular_size: MetaFieldDescriptor[int] = MetaFieldDescriptor(
        int, default=0
    )  # in bytes
    rootfs_directory: MetaFieldDescriptor[str] = MetaFieldDescriptor(
        str, default=_MUST_SET_CLAIM
    )
    compressed_rootfs_directory: MetaFieldDescriptor[str] = MetaFieldDescriptor(
        str, default=""
    )
    # sign certificate
    certificate: MetaFieldDescriptor[MetaFile] = MetaFieldDescriptor(
        MetaFile, default=_MUST_SET_CLAIM
    )
    # metadata files definition
    directory: MetaFieldDescriptor[MetaFile] = MetaFieldDescriptor(
        MetaFile, default=_MUST_SET_CLAIM
    )
    symboliclink: MetaFieldDescriptor[MetaFile] = MetaFieldDescriptor(
        MetaFile, default=_MUST_SET_CLAIM
    )
    regular: MetaFieldDescriptor[MetaFile] = MetaFieldDescriptor(
        MetaFile, default=_MUST_SET_CLAIM
    )
    persistent: MetaFieldDescriptor[MetaFile] = MetaFieldDescriptor(
        MetaFile, default=_MUST_SET_CLAIM
    )

    @classmethod
    def check_metadata_version(cls, claims: List[Dict[str, Any]]):
        """Check the <version> field in claims and issue warning if needed.

        Warnings will be issued for the following 2 cases:
        1. <version> field is missing,
        2. <version> doesn't match <SCHEME_VERSION>
        """
        version_field_found = False
        for entry in claims:
            # if entry is version, check version compatibility
            if cls.VERSION_KEY in entry:
                version_field_found = True
                if (version := entry[cls.VERSION_KEY]) != cls.SCHEME_VERSION:
                    logger.warning(f"metadata {version=}, expect {cls.SCHEME_VERSION}")
                break
        if not version_field_found:
            logger.warning("metadata version is missing in metadata.jwt!")

    @classmethod
    def parse_payload(cls, payload: Union[str, bytes], /) -> Self:
        # NOTE: in version1, payload is a list of dict,
        #   check module docstring for more details
        claims: List[Dict[str, Any]] = json.loads(payload)
        if not claims or not isinstance(claims, list):
            _err_msg = f"invalid {payload=}"
            logger.error(_err_msg)
            raise MetadataJWTPayloadInvalid(_err_msg)
        cls.check_metadata_version(claims)

        (res := cls()).assign_fields(claims)
        return res

    def assign_fields(self, claims: List[Dict[str, Any]]):
        """Assign each fields in _MetadataJWTClaimsLayout with input claims
        in parsed metadata.jwt."""
        for field in fields(self):
            # NOTE: default value for each field in dataclass is the descriptor
            # NOTE: skip non-metafield field
            if not isinstance((fd := field.default), MetaFieldDescriptor):
                continue

            field_assigned = False
            for claim in claims:
                if fd.check_is_target_claim(claim):
                    try:
                        setattr(self, fd.field_name, claim)
                        field_assigned = True
                        break
                    except ValueError as e:
                        _err_msg = (
                            f"failed to assign {fd.field_name} with {claim}: {e!r}"
                        )
                        logger.error(_err_msg)
                        raise MetadataJWTPayloadInvalid(_err_msg) from e

            # failed to find target claim in metadata.jwt, and
            # this field is MUST_SET field
            if not field_assigned and fd.default is _MUST_SET_CLAIM:
                _err_msg = f"must set field {fd.name} not found in metadata.jwt"
                logger.error(_err_msg)
                raise MetadataJWTPayloadInvalid(_err_msg)

    def get_img_metafiles(self) -> Iterator[MetaFile]:
        """Get the metafiles that describes the OTA image."""
        _otameta_cls = self.__class__
        for f in [
            _otameta_cls.directory,
            _otameta_cls.symboliclink,
            _otameta_cls.regular,
            _otameta_cls.persistent,
        ]:
            if (
                (fd := getattr(self.__class__, f.name))
                and isinstance(fd, MetaFieldDescriptor)
                and fd.field_type is MetaFile
            ):
                yield getattr(self, f.name)


# -------- otametadata ------ #


@dataclass
class DownloadInfo:
    url: str
    dst: str
    digest_alg: Optional[str] = None
    digest: Optional[str] = None


class OTAMetadata:
    """
    workdir layout:
    /
    - / .download # the download area for OTA image files
    - / file_table.sqlite3 # the file table generated from metafiles,
                           # this will be saved to standby slot.
    - / resource_table.sqlite3 # the resource table generated from metafiles.
    - / persists.txt # the persist files list.

    """

    ENTRY_POINT = "metadata.jwt"
    DIGEST_ALG = "sha256"
    PERSIST_FNAME = "persists.txt"

    FSTABLE_DB = "file_table.sqlite3"
    FSTABLE_DB_TABLE_NAME = "file_table"

    RSTABLE_DB = "resource_table.sqlite3"
    RSTABLE_DB_TABLE_NAME = "resource_table"

    def __init__(
        self,
        *,
        base_url: str,
        work_dir: StrOrPath,
        ca_chains_store: CAChainStore,
    ) -> None:
        if not ca_chains_store:
            _err_msg = "CA chains store is empty!!! immediately fail the verification"
            logger.error(_err_msg)
            raise MetadataJWTVerificationFailed(_err_msg)

        self._ca_store = ca_chains_store
        self._base_url = base_url
        self._work_dir = wd = Path(work_dir)
        wd.mkdir(exist_ok=True, parents=True)

        self._download_folder = df = self._work_dir / ".download"
        df.mkdir(exist_ok=True, parents=True)

    def download_metafiles(self) -> Generator[DownloadInfo, None, None]:
        try:
            # ------ step 1: download metadata.jwt ------ #
            _metadata_jwt_fpath = self._download_folder / self.ENTRY_POINT
            yield DownloadInfo(
                url=urljoin_ensure_base(self._base_url, self.ENTRY_POINT),
                dst=str(_metadata_jwt_fpath),
            )

            _parser = _MetadataJWTParser(
                _metadata_jwt_fpath.read_text(),
                ca_chains_store=self._ca_store,
            )

            # get not yet verified parsed ota_metadata
            _metadata_jwt = _parser.get_metadata_jwt()

            # ------ step 2: download the certificate itself ------ #
            cert_info = _metadata_jwt.certificate
            cert_fname, cert_hash = cert_info.file, cert_info.hash

            _cert_fpath = self._download_folder / cert_fname
            yield DownloadInfo(
                url=urljoin_ensure_base(self._base_url, cert_fname),
                dst=str(_cert_fpath),
                digest_alg=self.DIGEST_ALG,
                digest=cert_hash,
            )

            cert_bytes = _cert_fpath.read_bytes()
            _parser.verify_metadata_cert(cert_bytes)
            _parser.verify_metadata_signature(cert_bytes)

            # ------ step 3: download OTA image metafiles ------ #
            for _metafile in _metadata_jwt.get_img_metafiles():
                _fname, _digest = _metafile.file, _metafile.hash
                _meta_fpath = self._download_folder / _fname

                yield DownloadInfo(
                    url=urljoin_ensure_base(self._base_url, _fname),
                    dst=str(_meta_fpath),
                    digest_alg=self.DIGEST_ALG,
                    digest=_digest,
                )

            # ------ step 4: parse OTA image metafiles ------ #
            (self._work_dir / self.FSTABLE_DB).unlink(missing_ok=True)
            (self._work_dir / self.RSTABLE_DB).unlink(missing_ok=True)
            _ft_db_conn = sqlite3.connect(self._work_dir / self.FSTABLE_DB)
            _rs_db_conn = sqlite3.connect(self._work_dir / self.RSTABLE_DB)

            _ft_orm = FileSystemTableORM(_ft_db_conn)
            _ft_orm.orm_create_table()

            _rs_orm = ResourceTableORM(_rs_db_conn)
            _rs_orm.orm_create_table()

            try:
                parse_dirs_from_csv_file(
                    str(self._download_folder / _metadata_jwt.directory.file),
                    _ft_orm,
                )
                parse_regulars_from_csv_file(
                    str(self._download_folder / _metadata_jwt.regular.file),
                    _orm=_ft_orm,
                    _orm_rs=_rs_orm,
                )
                parse_symlinks_from_csv_file(
                    str(self._download_folder / _metadata_jwt.symboliclink.file),
                    _ft_orm,
                )
            except Exception as e:
                _err_msg = f"failed to parse CSV metafiles: {e!r}"
                logger.error(_err_msg)
                raise
            finally:
                _ft_db_conn.close()
                _rs_db_conn.close()

            # ------ step 5: persist files list ------ #
            _persist_meta = self._download_folder / _metadata_jwt.persistent.file
            shutil.move(str(_persist_meta), self._work_dir / self.PERSIST_FNAME)
        finally:
            shutil.rmtree(self._download_folder, ignore_errors=True)

    @property
    def fstable_db_fpath(self) -> str:
        return str(self._work_dir / self.FSTABLE_DB)

    @property
    def rstable_db_fpath(self) -> str:
        return str(self._work_dir / self.RSTABLE_DB)

    def connect_fstable(self, *, read_only: bool) -> sqlite3.Connection:
        _db_fpath = self._work_dir / self.FSTABLE_DB
        if read_only:
            return sqlite3.connect(f"file:{_db_fpath}?mode=ro", uri=True)
        return sqlite3.connect(_db_fpath)

    def connect_rstable(self, *, read_only: bool) -> sqlite3.Connection:
        _db_fpath = self._work_dir / self.RSTABLE_DB
        if read_only:
            return sqlite3.connect(f"file:{_db_fpath}?mode=ro", uri=True)
        return sqlite3.connect(_db_fpath)

    def save_fstable(self, dst: StrOrPath) -> None:
        shutil.copy(self._work_dir / self.FSTABLE_DB, dst)
