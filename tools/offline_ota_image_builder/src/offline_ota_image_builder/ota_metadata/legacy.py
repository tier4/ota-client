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

import base64
import json
import logging
import re
from dataclasses import dataclass, fields
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    ClassVar,
    Dict,
    Generic,
    Iterator,
    List,
    Self,
    TypeVar,
    Union,
    overload,
)

logger = logging.getLogger(__name__)


class OTAImageInvalid(Exception): ...


class MetadataJWTPayloadInvalid(Exception):
    """Raised when verification passed, but input metadata.jwt is invalid."""


class MetadataJWTVerificationFailed(Exception):
    pass


FV = TypeVar("FV")


class MetafilesV1(str, Enum):
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

    HASH_ALG = "sha256"

    def __init__(self, metadata_jwt: str, *, certs_dir: Union[str, Path]):
        self.cert_dir = Path(certs_dir)

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
        self.ota_metadata = _MetadataJWTClaimsLayout.parse_payload(self.payload_bytes)
        logger.info(f"metadata={self.ota_metadata!r}")

    def get_otametadata(self) -> "_MetadataJWTClaimsLayout":
        """Get parsed OTAMetaData.

        Returns:
            A instance of OTAMetaData representing the parsed(but not yet verified) metadata.
        """
        return self.ota_metadata


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


# ------ support for text based OTA metafiles parsing ------ #


@dataclass
class RegularInf:
    compressed_alg: str = ""
    """zst"""

    gid: int = 0
    inode: int = 0
    mode: int = 0
    nlink: int = 0
    path: str = ""
    sha256hash: bytes = b""
    size: int = 0
    uid: int = 0


def de_escape(s: str) -> str:
    return s.replace(r"'\''", r"'")


# format definition in regular pattern
# NOTE(20221013): support previous regular_inf cvs version
#                 that doesn't contain size, inode and compressed_alg fields.
_reginf_pa = re.compile(
    r"(?P<mode>\d+),(?P<uid>\d+),(?P<gid>\d+)"
    r",(?P<nlink>\d+),(?P<hash>\w+),'(?P<path>.+)'"
    r"(,(?P<size>\d+)?(,(?P<inode>\d+)?(,(?P<compressed_alg>\w+)?)?)?)?"
)


def parse_regulars_from_txt(_input: str) -> RegularInf:
    """Compatibility to the plaintext regulars.txt."""
    res = RegularInf()
    _ma = _reginf_pa.match(_input.strip())
    assert _ma is not None, f"matching reg_inf failed for {_input}"

    res.mode = int(_ma.group("mode"), 8)
    res.uid = int(_ma.group("uid"))
    res.gid = int(_ma.group("gid"))
    res.nlink = int(_ma.group("nlink"))
    res.sha256hash = bytes.fromhex(_ma.group("hash"))
    res.path = de_escape(_ma.group("path"))

    if _size := _ma.group("size"):
        res.size = int(_size)
        # ensure that size exists before parsing inode
        # and compressed_alg field.
        res.inode = int(_inode) if (_inode := _ma.group("inode")) else 0
        res.compressed_alg = (
            _compress_alg if (_compress_alg := _ma.group("compressed_alg")) else ""
        )

    return res


METADATA_JWT = "metadata.jwt"
