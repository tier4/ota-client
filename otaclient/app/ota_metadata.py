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

Version1 JWT verification algorithm: ES256
Version1 JWT payload layout:
[
    {"version": 1},
    {"directory": "dirs.txt", "hash": <sha256_hash>},
    {"symboliclink": "symlinks.txt", "hash": <sha256_hash>},
    {"regular": "regulars.txt", "hash": <sha256_hash>},
    {"persistent": "persistents.txt", "hash": <sha256_hash>},
    {"certificate": "sign.pem", "hash": <sha256_hash>},
    {"total_regular_size": "23637537004"},
    {"rootfs_directory": "data"},
    {"compressed_rootfs_directory": "data.zstd"}
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
import re
import shutil
import time
from enum import Enum
from os import PathLike
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, fields
from urllib.parse import quote
from OpenSSL import crypto
from pathlib import Path
from functools import partial
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import (
    Any,
    Callable,
    ClassVar,
    Dict,
    Generic,
    Iterator,
    List,
    Optional,
    Tuple,
    TypeVar,
    Type,
    Union,
    overload,
)
from typing_extensions import Self

from .configs import config as cfg
from .common import OTAFileCacheControl, RetryTaskMap, urljoin_ensure_base
from .downloader import Downloader
from .proto.wrapper import (
    MessageWrapper,
    RegularInf,
    DirectoryInf,
    PersistentInf,
    SymbolicLinkInf,
)
from .proto.streamer import Uint32LenDelimitedMsgReader, Uint32LenDelimitedMsgWriter
from . import log_setting

logger = log_setting.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)

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
    """
    Expecting field value like:
    - a dict with one key-value pair,
        { "version": 1 }
    or
    - a dict with 2 fields,
        {
            <metafile_name>: <metafile_file_name>,
            "hash": "faaea3ee13b4cc0e107ab2840f792d480636bec6b4d1ccd4a0a3ce8046b2c549"
        },
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
        if isinstance(value, type(self)):
            setattr(obj, self._attrn, self.default)
            return  # handle dataclass default value setting

        if isinstance(value, dict):
            # special treatment for MetaFile field
            if self.field_type is MetaFile:
                setattr(
                    obj,
                    self._attrn,
                    MetaFile(file=value[self.field_name], hash=value[self.HASH_KEY]),
                )
            else:
                # use <field_type> to do default conversion before assignment
                setattr(obj, self._attrn, self.field_type(value[self.field_name]))
        else:
            setattr(obj, self._attrn, self.field_type(value))

    def __set_name__(self, owner: type, name: str):
        self.name = name
        self.field_name = name
        self._attrn = f"_{owner.__name__}_{name}"


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

    def _verify_metadata_cert(self, metadata_cert: bytes) -> None:
        """Verify the metadata's sign certificate against local pinned CA.

        Raises:
            Raise ValueError on verification failed.
        """
        ca_set_prefix = set()
        # e.g. under _certs_dir: A.1.pem, A.2.pem, B.1.pem, B.2.pem
        for cert in self.cert_dir.glob("*.*.pem"):
            if m := re.match(r"(.*)\..*.pem", cert.name):
                ca_set_prefix.add(m.group(1))
            else:
                raise ValueError("no pem file is found")
        if len(ca_set_prefix) == 0:
            logger.warning("there is no root or intermediate certificate")
            return
        logger.info(f"certs prefixes {ca_set_prefix}")

        load_pem = partial(crypto.load_certificate, crypto.FILETYPE_PEM)

        try:
            cert_to_verify = load_pem(metadata_cert)
        except crypto.Error:
            logger.exception(f"invalid certificate {metadata_cert}")
            raise ValueError(f"invalid certificate {metadata_cert}")

        for ca_prefix in sorted(ca_set_prefix):
            certs_list = [
                self.cert_dir / c.name for c in self.cert_dir.glob(f"{ca_prefix}.*.pem")
            ]

            store = crypto.X509Store()
            for c in certs_list:
                logger.info(f"cert {c}")
                store.add_cert(load_pem(c.read_bytes()))

            try:
                store_ctx = crypto.X509StoreContext(store, cert_to_verify)
                store_ctx.verify_certificate()
                logger.info(f"verfication succeeded against: {ca_prefix}")
                return
            except crypto.X509StoreContextError as e:
                logger.info(f"verify against {ca_prefix} failed: {e}")

        logger.error(f"metadata sign certificate {metadata_cert} could not be verified")
        raise ValueError(
            f"metadata sign certificate {metadata_cert} could not be verified"
        )

    def _verify_metadata(self, metadata_cert: bytes):
        """Verify metadata against sign certificate.

        Raises:
            Raise ValueError on validation failed.
        """
        try:
            cert = crypto.load_certificate(crypto.FILETYPE_PEM, metadata_cert)
            logger.debug(f"verify data: {self.metadata_bytes=}")
            crypto.verify(
                cert,
                self.metadata_signature,
                self.metadata_bytes,
                self.HASH_ALG,
            )
        except crypto.Error as e:
            msg = f"failed to verify metadata against sign cert: {e!r}"
            logger.error(msg)
            raise ValueError(msg) from None

    def get_otametadata(self) -> "_MetadataJWTClaimsLayout":
        """Get parsed OTAMetaData.

        Returns:
            A instance of OTAMetaData representing the parsed(but not yet verified) metadata.
        """
        return self.ota_metadata

    def verify_metadata(self, metadata_cert: bytes):
        """Verify metadata_jwt against metadata cert and local pinned CA certs.

        Raises:
            Raise ValueError on verification failed.
        """
        # step1: verify the cert itself against local pinned CA cert
        self._verify_metadata_cert(metadata_cert)
        # step2: verify the metadata against input metadata_cert
        self._verify_metadata(metadata_cert)


@dataclass
class _MetadataJWTClaimsLayout:
    """Version1 metadata.jwt payload mapping and parsing."""

    SCHEME_VERSION: ClassVar[int] = 1
    VERSION_KEY: ClassVar[str] = "version"

    # metadata scheme
    version: MetaFieldDescriptor[int] = MetaFieldDescriptor(int)
    total_regular_size: MetaFieldDescriptor[int] = MetaFieldDescriptor(int)  # in bytes
    rootfs_directory: MetaFieldDescriptor[str] = MetaFieldDescriptor(str)
    compressed_rootfs_directory: MetaFieldDescriptor[str] = MetaFieldDescriptor(str)
    # sign certificate
    certificate: MetaFieldDescriptor[MetaFile] = MetaFieldDescriptor(MetaFile)
    # metadata files definition
    directory: MetaFieldDescriptor[MetaFile] = MetaFieldDescriptor(MetaFile)
    symboliclink: MetaFieldDescriptor[MetaFile] = MetaFieldDescriptor(MetaFile)
    regular: MetaFieldDescriptor[MetaFile] = MetaFieldDescriptor(MetaFile)
    persistent: MetaFieldDescriptor[MetaFile] = MetaFieldDescriptor(MetaFile)

    @classmethod
    def parse_payload(cls, _input: Union[str, bytes], /) -> Self:
        # NOTE: in version1, payload is a list of dict
        payload: List[Dict[str, Any]] = json.loads(_input)
        if not payload or not isinstance(payload, list):
            raise ValueError(f"invalid payload: {_input}")

        res = cls()
        for entry in payload:
            # if entry is version, check version compatibility
            if (
                cls.VERSION_KEY in entry
                and entry[cls.VERSION_KEY] != cls.SCHEME_VERSION
            ):
                logger.warning(f"metadata version is {entry['version']}.")
            # parse other fields
            for f in fields(cls):
                if (fn := f.name) in entry:
                    setattr(res, fn, entry)
        return res

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


def de_escape(s: str) -> str:
    return s.replace(r"'\''", r"'")


# format definition in regular pattern

_dir_pa = re.compile(r"(?P<mode>\d+),(?P<uid>\d+),(?P<gid>\d+),(?P<path>.*)")
_symlink_pa = re.compile(
    r"(?P<mode>\d+),(?P<uid>\d+),(?P<gid>\d+),'(?P<link>.+)((?<!\')',')(?P<target>.+)'"
)
_persist_pa = re.compile(r"'(?P<path>)'")
# NOTE(20221013): support previous regular_inf cvs version
#                 that doesn't contain size, inode and compressed_alg fields.
_reginf_pa = re.compile(
    r"(?P<mode>\d+),(?P<uid>\d+),(?P<gid>\d+)"
    r",(?P<nlink>\d+),(?P<hash>\w+),'(?P<path>.+)'"
    r"(,(?P<size>\d+)?(,(?P<inode>\d+)?(,(?P<compressed_alg>\w+)?)?)?)?"
)


def parse_dirs_from_txt(_input: str) -> DirectoryInf:
    """Compatibility to the plaintext dirs.txt."""
    _ma = _dir_pa.match(_input.strip())
    assert _ma is not None, f"matching dirs failed for {_input}"

    mode = int(_ma.group("mode"), 8)
    uid = int(_ma.group("uid"))
    gid = int(_ma.group("gid"))
    path = de_escape(_ma.group("path")[1:-1])
    return DirectoryInf(mode=mode, uid=uid, gid=gid, path=path)


def parse_persistents_from_txt(_input: str) -> PersistentInf:
    """Compatibility to the plaintext persists.txt."""
    _path = de_escape(_input.strip()[1:-1])
    return PersistentInf(path=_path)


def parse_symlinks_from_txt(_input: str) -> SymbolicLinkInf:
    """Compatibility to the plaintext symlinks.txt."""
    _ma = _symlink_pa.match(_input.strip())
    assert _ma is not None, f"matching symlinks failed for {_input}"

    res = SymbolicLinkInf()
    res.mode = int(_ma.group("mode"), 8)
    res.uid = int(_ma.group("uid"))
    res.gid = int(_ma.group("gid"))
    res.slink = de_escape(_ma.group("link"))
    res.srcpath = de_escape(_ma.group("target"))
    return res


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


# -------- otametadata ------ #


@dataclass
class MetafileParserMapping:
    text_parser: Callable[[str], MessageWrapper]
    bin_fname: str
    wrapper_type: Type[MessageWrapper]


class OTAMetadata:
    """Implementation of loading/parsing OTA metadata.jwt and metafiles."""

    METADATA_JWT = "metadata.jwt"

    # internal used binary metafiles fname
    REGULARS_BIN = "regulars.bin"
    DIRS_BIN = "dirs.bin"
    SYMLINKS_BIN = "symlinks.bin"
    PERSISTENTS_BIN = "persistents.bin"

    # metafile fname <-> parser mapping
    METAFILE_PARSER_MAPPING = {
        MetafilesV1.DIRECTORY_FNAME: MetafileParserMapping(
            parse_dirs_from_txt,
            DIRS_BIN,
            DirectoryInf,
        ),
        MetafilesV1.SYMBOLICLINK_FNAME: MetafileParserMapping(
            parse_symlinks_from_txt,
            SYMLINKS_BIN,
            SymbolicLinkInf,
        ),
        MetafilesV1.REGULAR_FNAME: MetafileParserMapping(
            parse_regulars_from_txt,
            REGULARS_BIN,
            RegularInf,
        ),
        MetafilesV1.PERSISTENT_FNAME: MetafileParserMapping(
            parse_persistents_from_txt,
            PERSISTENTS_BIN,
            PersistentInf,
        ),
    }

    def __init__(self, *, url_base: str, downloader: Downloader) -> None:
        self.url_base = url_base
        self._downloader = downloader
        self._tmp_dir = TemporaryDirectory(prefix="ota_metadata", dir=cfg.RUN_DIR)
        self._tmp_dir_path = Path(self._tmp_dir.name)

        # download and parse the metadata.jwt
        self.scheme_version = _MetadataJWTClaimsLayout.SCHEME_VERSION
        self._ota_metadata = self._process_metadata_jwt()
        self.image_rootfs_url = urljoin_ensure_base(
            self.url_base, f"{self._ota_metadata.rootfs_directory.strip('/')}/"
        )
        self.image_compressed_rootfs_url = urljoin_ensure_base(
            self.url_base,
            f"{self._ota_metadata.compressed_rootfs_directory.strip('/')}/",
        )
        self.total_regular_size = self._ota_metadata.total_regular_size
        # download, parse and store ota metatfiles
        self._process_text_base_otameta_files()

    def _process_metadata_jwt(self) -> _MetadataJWTClaimsLayout:
        """Download, loading and parsing metadata.jwt."""
        logger.debug("process metadata.jwt...")
        # download and parse metadata.jwt
        with NamedTemporaryFile(prefix="metadata_jwt", dir=cfg.RUN_DIR) as meta_f:
            _downloaded_meta_f = Path(meta_f.name)
            self._downloader.download(
                urljoin_ensure_base(self.url_base, self.METADATA_JWT),
                _downloaded_meta_f,
                # NOTE: do not use cache when fetching metadata.jwt
                headers={
                    OTAFileCacheControl.header_lower.value: OTAFileCacheControl.no_cache.value,
                },
            )

            _parser = _MetadataJWTParser(
                _downloaded_meta_f.read_text(), certs_dir=cfg.CERTS_DIR
            )
        # get not yet verified parsed ota_metadata
        _ota_metadata = _parser.get_otametadata()

        # download certificate and verify metadata against this certificate
        with NamedTemporaryFile(prefix="metadata_cert", dir=cfg.RUN_DIR) as cert_f:
            cert_info = _ota_metadata.certificate
            cert_fname, cert_hash = cert_info.file, cert_info.hash
            cert_file = Path(cert_f.name)
            self._downloader.download(
                urljoin_ensure_base(self.url_base, cert_fname),
                cert_file,
                digest=cert_hash,
                headers={
                    OTAFileCacheControl.header_lower.value: OTAFileCacheControl.no_cache.value,
                },
            )
            _parser.verify_metadata(cert_file.read_bytes())

        # return verified ota metadata
        return _ota_metadata

    def _process_text_base_otameta_files(self):
        """Downloading, loading and parsing metafiles."""
        logger.debug("process ota metafiles...")

        def _process_text_base_otameta_file(_metafile: MetaFile):
            _parser_info = self.METAFILE_PARSER_MAPPING[MetafilesV1(_metafile.file)]
            with NamedTemporaryFile(prefix=f"metafile_{_metafile.file}") as _metafile_f:
                _metafile_fpath = Path(_metafile_f.name)
                self._downloader.download(
                    urljoin_ensure_base(self.url_base, quote(_metafile.file)),
                    _metafile_fpath,
                    digest=_metafile.hash,
                    headers={
                        OTAFileCacheControl.header_lower.value: OTAFileCacheControl.no_cache.value
                    },
                )
                # convert to internal used version and store as binary files
                with open(_metafile_fpath, "r") as _src_f, open(
                    Path(self._tmp_dir.name) / _parser_info.bin_fname, "wb"
                ) as _dst_f:
                    exporter = Uint32LenDelimitedMsgWriter(
                        _dst_f, _parser_info.wrapper_type
                    )
                    for _line in _src_f:
                        exporter.write1_msg(_parser_info.text_parser(_line))

        _keep_failing_timer = time.time()
        with ThreadPoolExecutor(thread_name_prefix="process_metafiles") as _executor:
            _mapper = RetryTaskMap(
                _process_text_base_otameta_file,
                self._ota_metadata.get_img_metafiles(),
                title="process_metafiles",
                max_concurrent=cfg.MAX_CONCURRENT_DOWNLOAD_TASKS,
                backoff_max=cfg.DOWNLOAD_GROUP_BACKOFF_MAX,
                backoff_factor=cfg.DOWNLOAD_GROUP_BACKOFF_FACTOR,
                max_retry=0,  # NOTE: we use another strategy below
                executor=_executor,
            )
            for _exp, _entry, _ in _mapper.execute():
                if not isinstance(_exp, Exception):
                    _keep_failing_timer = time.time()
                    continue

                logger.debug(f"metafile downloading failed: {_entry=}")
                if (
                    time.time() - _keep_failing_timer
                    > cfg.DOWNLOAD_GROUP_NO_SUCCESS_RETRY_TIMEOUT
                ):
                    _mapper.shutdown()

    # APIs

    def save_metafiles_bin_to(self, dst_dir: PathLike):
        for _, _inf_files_mapping in self.METAFILE_PARSER_MAPPING.items():
            _fname = _inf_files_mapping.bin_fname
            _fpath = self._tmp_dir_path / _fname
            shutil.copy(_fpath, dst_dir)

    def iter_metafile(self, metafile: MetafilesV1) -> Iterator[Any]:
        _parser_info = self.METAFILE_PARSER_MAPPING[metafile]
        with open(self._tmp_dir_path / _parser_info.bin_fname, "rb") as _f:
            _stream_reader = Uint32LenDelimitedMsgReader(_f, _parser_info.wrapper_type)
            yield from _stream_reader.iter_msg()

    def get_download_url(self, reg_inf: RegularInf) -> Tuple[str, Optional[str]]:
        """
        NOTE: compressed file is located under another OTA image remote folder

        Returns:
            A tuple of download url and zstd_compression enable flag.
        """
        # v2 OTA image, with compression enabled
        # example: http://example.com/base_url/data.zstd/a665a45920422f9d417e4867efdc4fb8a04a1f3fff1fa07e998e86f7f7a27ae3.<compression_alg>
        if (
            reg_inf.compressed_alg
            and reg_inf.compressed_alg in cfg.SUPPORTED_COMPRESS_ALG
        ):
            return (
                urljoin_ensure_base(
                    self.image_compressed_rootfs_url,
                    # NOTE: hex alpha-digits and dot(.) are not special character
                    #       so no need to use quote here.
                    f"{reg_inf.get_hash()}.{reg_inf.compressed_alg}",
                ),
                reg_inf.compressed_alg,
            )
        # v1 OTA image, uncompressed and use full path as URL path
        # example: http://example.com/base_url/data/rootfs/full/path/file
        else:
            return (
                urljoin_ensure_base(
                    self.image_rootfs_url, quote(str(reg_inf.relative_to("/")))
                ),
                None,
            )
