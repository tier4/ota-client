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


from __future__ import annotations
import base64
import json
import re
import shutil
import time
from os import PathLike
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, fields
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
    Iterable,
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
from .proto.streamer import Uint32LenDelimitedReader, Uint32LenDelimitedWriter
from . import log_setting

logger = log_setting.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)

FV = TypeVar("FV")


@dataclass
class MetaFile:
    file: str
    hash: str

    def asdict(self):
        return asdict(self)


class MetaFieldDescriptor(Generic[FV]):
    HASH_KEY = "hash"  # version1

    def __init__(self, field_type: type, *, default=None) -> None:
        self.field_type = field_type
        # NOTE: if field not presented, set to None
        self.default = default

    @overload
    def __get__(self, obj: None, objtype: type) -> "MetaFieldDescriptor":
        """Descriptor accessed via class."""
        ...

    @overload
    def __get__(self, obj, objtype: type) -> FV:
        """Descriptor accessed via bound instance."""
        ...

    def __get__(self, obj, objtype=None) -> "Union[FV, MetaFieldDescriptor]":
        if obj and not isinstance(obj, type):
            return getattr(obj, self._private_name)  # access via instance
        return self  # access via class, return the descriptor

    def __set__(self, obj, value: Any) -> None:
        if isinstance(value, type(self)):
            setattr(obj, self._private_name, self.default)
            return  # handle dataclass default value setting

        if isinstance(value, dict):
            """
            expecting input like
                {
                    "version": 1
                }
            or
                {
                    "symboliclink": "symlinks.txt",
                    "hash": "faaea3ee13b4cc0e107ab2840f792d480636bec6b4d1ccd4a0a3ce8046b2c549"
                },
            """
            # special treatment for MetaFile field
            if self.field_type is MetaFile:
                setattr(
                    obj,
                    self._private_name,
                    MetaFile(file=value[self._file_fn], hash=value[self.HASH_KEY]),
                )
            else:
                # use <field_type> to do default conversion before assignment
                setattr(obj, self._private_name, self.field_type(value[self._file_fn]))
        else:
            setattr(obj, self._private_name, self.field_type(value))

    def __set_name__(self, owner: type, name: str):
        self.owner = owner
        self.name = name
        self._file_fn = name
        self._private_name = f"_{owner.__name__}_{name}"


class _MetadataJWTParser:
    """Implementation of custom JWT parsing/loading/validating logic.

    metadata.jwt version1 payload layout:
    [
        {"version": 1},
        {
            "file": "regular.txt",
            "hash": <sha256hash_hex_string>,
        }
        # other metafiles definition
        ...
    ]

    Certification based JWT verification:
        The root and intermediate certificates exist under certs_dir.
        When A.root.pem, A.intermediate.pem, B.root.pem, and B.intermediate.pem
        exist, the groups of A* and the groups of B* are handled as a chained
        certificate.
        verify function verifies specified certificate with them.
        Certificates file name format should be: '.*\\..*.pem'

        NOTE:
            If there is no root or intermediate certificate, certification verification
            will be skipped!
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
        self.ota_metadata = _MetadataJWTClaims.parse_payload(self.payload_bytes)
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

    def get_otametadata(self) -> "_MetadataJWTClaims":
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
class _MetadataJWTClaims:
    """metadata.jwt payload mapping and parsing.

    In version1, we have image metafiles as follow:
        - directory: all directories in the image,
        - symboliclink: all symlinks in the image,
        - regular: all normal/regular files in the image,
        - persistent: files that should be preserved across update.
    """

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
    def parse_payload(cls, _input: Union[str, bytes]) -> Self:
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

MetaInfo = namedtuple(
    "MetaInfo",
    [
        "metadata_scheme_version",
        "total_regular_size",
        "rootfs_directory",
        "compressed_rootfs_directory",
    ],
)


@dataclass
class MetafileParserMapping:
    text_parser: Callable
    bin_fname: str
    wrapper_type: Type[MessageWrapper]


class OTAMetadata:
    """Implementation of loading/parsing OTA metadata.jwt and metafiles."""

    METADATA_JWT = "metadata.jwt"

    REGULARS_INF_BIN_FNAME = "regulars.bin"
    DIRECTORYS_INF_BIN_FNAME = "dirs.bin"
    SYMLINKS_INF_BIN_FNAME = "symlinks.bin"
    PERSISTENTS_INF_BIN_FNAME = "persistents.bin"

    METAFILE_PARSER_MAPPING = {
        "directory": MetafileParserMapping(
            parse_dirs_from_txt, DIRECTORYS_INF_BIN_FNAME, DirectoryInf
        ),
        "symbolicklink": MetafileParserMapping(
            parse_symlinks_from_txt,
            SYMLINKS_INF_BIN_FNAME,
            SymbolicLinkInf,
        ),
        "regular": MetafileParserMapping(
            parse_regulars_from_txt, REGULARS_INF_BIN_FNAME, RegularInf
        ),
        "persistent": MetafileParserMapping(
            parse_persistents_from_txt,
            PERSISTENTS_INF_BIN_FNAME,
            PersistentInf,
        ),
    }

    def __init__(
        self,
        *,
        url_base: str,
        downloader: Downloader,
        cookies: Dict[str, str],
        proxies: Dict[str, str],
    ) -> None:
        self._url_base = url_base
        self._downloader = downloader
        self._cookies = cookies.copy()
        self._proxies = proxies.copy()
        self._tmp_dir = TemporaryDirectory(prefix="ota_metadata", dir=cfg.RUN_DIR)
        self._tmp_dir_path = Path(self._tmp_dir.name)

        self._ota_metadata: _MetadataJWTClaims = None  # type: ignore

        # download and parsing the metadata.jwt and ota metafiles
        self._process_metadata_jwt()
        self._parse_text_base_otameta_files()

    def _process_metadata_jwt(self) -> None:
        """Custom parsing jwt logic."""
        logger.debug("process metadata jwt...")
        with NamedTemporaryFile(
            prefix="otaclient_metadata_jwt", dir=cfg.RUN_DIR
        ) as meta_f:
            metadata_jwt_url = urljoin_ensure_base(self._url_base, self.METADATA_JWT)

            _downloaded_meta_f = Path(meta_f.name)
            self._downloader.download(
                metadata_jwt_url,
                _downloaded_meta_f,
                cookies=self._cookies,
                proxies=self._proxies,
                # NOTE: do not use cache when fetching metadata.jwt
                headers={
                    OTAFileCacheControl.header_lower.value: OTAFileCacheControl.no_cache.value,
                },
            )

            _parser = _MetadataJWTParser(
                _downloaded_meta_f.read_text(), certs_dir=cfg.CERTS_DIR
            )
            _ota_metadata = _parser.get_otametadata()

        # download certificate and verify metadata against this certificate
        with NamedTemporaryFile(prefix="metadata_cert", dir=cfg.RUN_DIR) as cert_f:
            cert_info = _ota_metadata.certificate
            cert_fname, cert_hash = cert_info.file, cert_info.hash
            cert_file = Path(cert_f.name)
            self._downloader.download(
                urljoin_ensure_base(self._url_base, cert_fname),
                cert_file,
                digest=cert_hash,
                cookies=self._cookies,
                proxies=self._proxies,
                headers={
                    OTAFileCacheControl.header_lower.value: OTAFileCacheControl.no_cache.value,
                },
            )
            _parser.verify_metadata(cert_file.read_bytes())

        self._ota_metadata = _ota_metadata

    def _parse_text_base_otameta_files(self):
        def _parse_text_base_otameta_file(_metafile: MetaFile):
            meta_f_url = urljoin_ensure_base(self._url_base, quote(_metafile.file))
            _parser_info = self.METAFILE_PARSER_MAPPING[_metafile.file]

            with NamedTemporaryFile(prefix=f"metafile_{_metafile.file}") as _metafile_f:
                _metafile_fpath = Path(_metafile_f.name)
                self._downloader.download(
                    meta_f_url,
                    _metafile_fpath,
                    digest=_metafile.hash,
                    proxies=self._proxies,
                    cookies=self._cookies,
                    headers={
                        OTAFileCacheControl.header_lower.value: OTAFileCacheControl.no_cache.value
                    },
                )
                with open(_metafile_fpath, "r") as _src_f, open(
                    Path(self._tmp_dir.name) / _parser_info.bin_fname, "wb"
                ) as _dst_f:
                    exporter = Uint32LenDelimitedWriter(
                        _dst_f, _parser_info.wrapper_type
                    )
                    for _line in _src_f:
                        exporter.write1_msg(_parser_info.text_parser(_line))

        _keep_failing_timer = time.time()
        with ThreadPoolExecutor(
            thread_name_prefix="parsing_ota_metafiles"
        ) as _executor:
            _mapper = RetryTaskMap(
                _parse_text_base_otameta_file,
                self._ota_metadata.get_img_metafiles(),
                title="parsing_ota_metafiles",
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

    def get_update_info(self) -> MetaInfo:
        """Get some information contained in the OTA metadata jwt claims."""
        return MetaInfo(
            total_regular_size=self._ota_metadata.total_regular_size,
            metadata_scheme_version=self._ota_metadata.version,
            rootfs_directory=self._ota_metadata.rootfs_directory,
            compressed_rootfs_directory=self._ota_metadata.compressed_rootfs_directory,
        )

    def save_metafiles_bin_to(self, dst_dir: PathLike):
        for _inf_files_mapping in self.METAFILE_PARSER_MAPPING:
            _fname = _inf_files_mapping[1]
            _fpath = self._tmp_dir_path / _fname
            shutil.copy(_fpath, dst_dir)

    def iter_regulars_inf(self) -> Iterable[RegularInf]:
        with open(self._tmp_dir_path / self.REGULARS_INF_BIN_FNAME, "rb") as _f:
            _stream_reader = Uint32LenDelimitedReader(_f, RegularInf)
            yield from _stream_reader.iter_msg()

    def iter_dirs_inf(self) -> Iterable[DirectoryInf]:
        with open(self._tmp_dir_path / self.DIRECTORYS_INF_BIN_FNAME, "rb") as _f:
            _stream_reader = Uint32LenDelimitedReader(_f, DirectoryInf)
            yield from _stream_reader.iter_msg()

    def iter_symlinks_inf(self) -> Iterable[SymbolicLinkInf]:
        with open(self._tmp_dir_path / self.SYMLINKS_INF_BIN_FNAME, "rb") as _f:
            _stream_reader = Uint32LenDelimitedReader(_f, SymbolicLinkInf)
            yield from _stream_reader.iter_msg()

    def iter_persistents_inf(self) -> Iterable[PersistentInf]:
        with open(self._tmp_dir_path / self.PERSISTENTS_INF_BIN_FNAME, "rb") as _f:
            _stream_reader = Uint32LenDelimitedReader(_f, PersistentInf)
            yield from _stream_reader.iter_msg()

    def get_image_data_url(self, base_url: str) -> str:
        if getattr(self, "_data_url", None) is None:
            self._data_url = urljoin_ensure_base(
                base_url, f"{self._ota_metadata.rootfs_directory.strip('/')}/"
            )
        return self._data_url

    def get_image_compressed_data_url(self, base_url: str) -> str:
        if getattr(self, "_compressed_data_url", None) is None:
            self._compressed_data_url = urljoin_ensure_base(
                base_url,
                f"{self._ota_metadata.compressed_rootfs_directory.strip('/')}/",
            )
        return self._compressed_data_url

    def get_download_url(
        self, reg_inf: RegularInf, *, base_url: str
    ) -> Tuple[str, Optional[str]]:
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
                    self.get_image_compressed_data_url(base_url),
                    quote(f"{reg_inf.get_hash()}.{reg_inf.compressed_alg}"),
                ),
                reg_inf.compressed_alg,
            )
        # v1 OTA image, uncompressed and use full path as URL path
        # example: http://example.com/base_url/data/rootfs/full/path/file
        else:
            return (
                urljoin_ensure_base(
                    self.get_image_data_url(base_url),
                    quote(str(reg_inf.relative_to("/"))),
                ),
                None,
            )
