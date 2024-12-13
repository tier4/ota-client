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

import logging
import os.path
import shutil
import sqlite3
import threading
from pathlib import Path
from typing import Callable, Generator
from urllib.parse import quote

from simple_sqlite3_orm.utils import sort_and_replace

from ota_metadata.file_table import (
    FTNonRegularORM,
    FTRegularORM,
)
from ota_metadata.file_table._orm import FTDirORM
from ota_metadata.file_table._table import (
    FileTableDirectories,
    FileTableNonRegularFiles,
    FileTableRegularFiles,
)
from ota_metadata.utils import DownloadInfo
from ota_metadata.utils.cert_store import CAChainStore
from otaclient_common.common import urljoin_ensure_base
from otaclient_common.typing import StrOrPath

from . import DIGEST_ALG, SUPORTED_COMPRESSION_TYPES
from .csv_parser import (
    parse_dirs_from_csv_file,
    parse_regulars_from_csv_file,
    parse_symlinks_from_csv_file,
)
from .parser import (
    MetadataJWTParser,
    MetadataJWTVerificationFailed,
    _MetadataJWTClaimsLayout,
)
from .rs_table import RSTORM, ResourceTable

logger = logging.getLogger(__name__)

# NOTE: enlarge the connection timeout on waiting db lock.
DB_TIMEOUT = 16  # seconds


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
    FSTABLE_DB = "file_table.sqlite3"
    RSTABLE_DB = "resource_table.sqlite3"

    FSTABLE_DB_NAME = "ft_in_memory"
    RSTABLE_DB_NAME = "rst_in_memory"

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

        self._download_folder = df = Path(work_dir) / f".download_{os.urandom(4).hex()}"
        df.mkdir(exist_ok=True, parents=True)

        self._fst_conn_weakref: set[sqlite3.Connection] = set()
        self._rst_conn_wearkref: set[sqlite3.Connection] = set()
        # NOTE(20241213): for performance consideration, we now use in-memory databases.
        # NOTE: keep at least one open connection all the time to prevent db being gced.
        self._fst_conn = self.connect_fstable()
        self._rst_conn = self.connect_rstable()

        self._metadata_jwt = None
        self._total_regulars_num = 0

    @property
    def metadata_jwt(self) -> _MetadataJWTClaimsLayout:
        assert self._metadata_jwt, "metadata_jwt is not ready yet!"
        return self._metadata_jwt

    @property
    def total_regulars_num(self) -> int:
        return self._total_regulars_num

    def download_metafiles(
        self,
        condition: threading.Condition,
        failed_flag: threading.Event,
    ) -> Generator[DownloadInfo, None, None]:
        """Guide the caller to download metadata files by yielding the DownloadInfo instances.

        While the caller downloading the metadata files one by one, this method
            will parse and verify the metadata.
        """
        try:
            # ------ step 1: download metadata.jwt ------ #
            _metadata_jwt_fpath = self._download_folder / self.ENTRY_POINT
            with condition:
                yield DownloadInfo(
                    url=urljoin_ensure_base(self._base_url, self.ENTRY_POINT),
                    dst=_metadata_jwt_fpath,
                )
                condition.wait()  # wait for download finished
            if failed_flag.is_set():
                return  # let the upper caller handles the failure

            _parser = MetadataJWTParser(
                _metadata_jwt_fpath.read_text(),
                ca_chains_store=self._ca_store,
            )

            # get not yet verified parsed ota_metadata
            _metadata_jwt = _parser.get_metadata_jwt()

            # ------ step 2: download the certificate itself ------ #
            cert_info = _metadata_jwt.certificate
            cert_fname, cert_hash = cert_info.file, cert_info.hash

            _cert_fpath = self._download_folder / cert_fname
            with condition:
                yield DownloadInfo(
                    url=urljoin_ensure_base(self._base_url, cert_fname),
                    dst=_cert_fpath,
                    digest_alg=self.DIGEST_ALG,
                    digest=cert_hash,
                )
                condition.wait()  # wait for download finished
            if failed_flag.is_set():
                return  # let the upper caller handles the failure

            cert_bytes = _cert_fpath.read_bytes()
            _parser.verify_metadata_cert(cert_bytes)
            _parser.verify_metadata_signature(cert_bytes)

            # only after the verification, assign the jwt to self
            self._metadata_jwt = _metadata_jwt

            # ------ step 3: download OTA image metafiles ------ #
            for _metafile in _metadata_jwt.get_img_metafiles():
                _fname, _digest = _metafile.file, _metafile.hash
                _meta_fpath = self._download_folder / _fname

                with condition:
                    yield DownloadInfo(
                        url=urljoin_ensure_base(self._base_url, _fname),
                        dst=_meta_fpath,
                        digest_alg=self.DIGEST_ALG,
                        digest=_digest,
                    )
                    condition.wait()  # wait for download finished
                if failed_flag.is_set():
                    return  # let the upper caller handles the failure

            # ------ step 4: parse OTA image metafiles ------ #
            _ft_regular_orm = FTRegularORM(self._fst_conn)
            _ft_regular_orm.orm_create_table()
            _ft_dir_orm = FTDirORM(self._fst_conn)
            _ft_dir_orm.orm_create_table()
            _ft_non_regular_orm = FTNonRegularORM(self._fst_conn)
            _ft_non_regular_orm.orm_create_table()

            _rs_orm = RSTORM(self._rst_conn)
            _rs_orm.orm_create_table()
            try:
                _dirs_num = parse_dirs_from_csv_file(
                    str(self._download_folder / _metadata_jwt.directory.file),
                    _ft_dir_orm,
                )
                _symlinks_num = parse_symlinks_from_csv_file(
                    str(self._download_folder / _metadata_jwt.symboliclink.file),
                    _ft_non_regular_orm,
                )
                self._total_regulars_num = _regulars_num = parse_regulars_from_csv_file(
                    str(self._download_folder / _metadata_jwt.regular.file),
                    _orm=_ft_regular_orm,
                    _orm_rs=_rs_orm,
                )
                logger.info(
                    f"csv parse finished: {_dirs_num=}, {_symlinks_num=}, {_regulars_num=}"
                )

                # NOTE: also check file_table definition at ota_metadata.file_table._table
                sort_and_replace(
                    _ft_regular_orm,  # type: ignore
                    _ft_regular_orm.table_name,
                    order_by_col="digest",
                )
            except Exception as e:
                _err_msg = f"failed to parse CSV metafiles: {e!r}"
                logger.error(_err_msg)

                # immediately close the in-memory db on failure to release memory
                self._fst_conn.close()
                self._rst_conn.close()
                raise

            # ------ step 5: persist files list ------ #
            _persist_meta = self._download_folder / _metadata_jwt.persistent.file
            shutil.move(str(_persist_meta), self._work_dir)
        finally:
            shutil.rmtree(self._download_folder, ignore_errors=True)

    # helper methods

    def iter_persist_entries(self) -> Generator[str]:
        _persist_fpath = self._work_dir / self.metadata_jwt.persistent.file
        with open(_persist_fpath, "r") as f:
            for line in f:
                yield line.strip()[1:-1]

    def iter_dir_entries(
        self, *, batch_size: int = 256
    ) -> Generator[FileTableDirectories]:
        _conn = self.connect_fstable()
        _ft_dir_orm = FTDirORM(_conn)
        try:
            yield from _ft_dir_orm.orm_select_all_with_pagination(batch_size=batch_size)
        finally:
            _conn.close()

    def iter_non_regular_entries(
        self, *, batch_size: int = 256
    ) -> Generator[FileTableNonRegularFiles]:
        _conn = self.connect_fstable()
        _ft_dir_orm = FTNonRegularORM(_conn)
        try:
            yield from _ft_dir_orm.orm_select_all_with_pagination(batch_size=batch_size)
        finally:
            _conn.close()

    def iter_regular_entries(
        self, *, batch_size: int = 256
    ) -> Generator[FileTableRegularFiles]:
        _conn = self.connect_fstable()
        _ft_dir_orm = FTRegularORM(_conn)
        try:
            yield from _ft_dir_orm.orm_select_all_with_pagination(batch_size=batch_size)
        finally:
            _conn.close()

    def connect_fstable(self) -> sqlite3.Connection:
        """NOTE: this method must be called in the main thread."""
        _uri = f"file:{self.FSTABLE_DB_NAME}?mode=memory&cache=shared"
        _conn = sqlite3.connect(
            _uri,
            uri=True,
            check_same_thread=False,
            timeout=DB_TIMEOUT,
        )
        self._fst_conn_weakref.add(_conn)
        return _conn

    def connect_rstable(self) -> sqlite3.Connection:
        """NOTE: this method must be called in the main thread."""
        _uri = f"file:{self.RSTABLE_DB_NAME}?mode=memory&cache=shared"
        _conn = sqlite3.connect(
            _uri,
            uri=True,
            check_same_thread=False,
            timeout=DB_TIMEOUT,
        )
        self._rst_conn_wearkref.add(_conn)
        return _conn

    def save_fstable(self, dst: StrOrPath) -> None:
        """TODO: implement me!"""
        # shutil.copy(self._work_dir / self.FSTABLE_DB, dst)

    def close_all_rst_conns(self) -> None:
        for _conn in self._rst_conn_wearkref:
            _conn.close()

    def close_all_fst_conns(self) -> None:
        for _conn in self._fst_conn_weakref:
            _conn.close()


def conns_factory(
    cons_num: int, *, con_maker: Callable[[], sqlite3.Connection]
) -> Callable[[], sqlite3.Connection]:
    _conns = [con_maker() for _ in range(cons_num)]

    def _inner():
        return _conns.pop()

    return _inner


class ResourceMeta:

    def __init__(
        self,
        *,
        base_url: str,
        ota_metadata: OTAMetadata,
        copy_dst: Path,
    ) -> None:
        self._ota_metadata = ota_metadata

        self.base_url = base_url
        self.data_dir_url = urljoin_ensure_base(
            base_url, ota_metadata.metadata_jwt.rootfs_directory
        )
        self.compressed_data_dir_url = None
        if _compressed_data := ota_metadata.metadata_jwt.compressed_rootfs_directory:
            self.compressed_data_dir_url = urljoin_ensure_base(
                base_url, _compressed_data
            )

        self._copy_dst = copy_dst

        self.download_list_len = self._get_download_list_len()
        self.download_size = self._get_download_size()

    def _get_download_list_len(self) -> int:
        _conn = self._ota_metadata.connect_rstable()
        _orm = RSTORM(_conn)
        _sql_stmt = ResourceTable.table_select_stmt(
            select_from=_orm.table_name,
            function="count",
        )

        try:
            _query = _orm.orm_con.execute(_sql_stmt)
            _raw_res = _query.fetchone()
            # NOTE: return value of fetchone will be a tuple, and here
            #   the first and only value of the tuple is the total nums of entries.
            assert isinstance(_raw_res, tuple) and _raw_res
            return _raw_res[0]
        finally:
            _conn.close()

    def _get_download_size(self):
        _conn = self._ota_metadata.connect_rstable()
        _orm = RSTORM(_conn)

        _sql_stmt = ResourceTable.table_select_stmt(
            select_from=_orm.table_name,
            select_cols=("original_size",),
            function="sum",
        )

        try:
            _query = _orm.orm_con.execute(_sql_stmt)
            _raw_res = _query.fetchone()
            # NOTE: return value of fetchone will be a tuple, and here
            #   the first and only value of the tuple is the total nums of entries.
            assert isinstance(_raw_res, tuple) and _raw_res
            return _raw_res[0]
        finally:
            _conn.close()

    # API

    def get_download_info(self, resource: ResourceTable) -> DownloadInfo:
        """Get DownloadInfo from one ResourceTable entry.

        Returns:
            An instance of DownloadInfo to download the resource indicates by <resource>.
        """
        assert (_digest := resource.digest), f"invalid {resource=}"
        _digest_str = _digest.hex()

        # v2 OTA image, with compression enabled
        # example: http://example.com/base_url/data.zstd/a665a45920422f9d417e4867efdc4fb8a04a1f3fff1fa07e998e86f7f7a27ae3.<compression_alg>
        if (
            self.compressed_data_dir_url
            and (_compress_alg := resource.compression_alg)
            in SUPORTED_COMPRESSION_TYPES
        ):
            return DownloadInfo(
                url=urljoin_ensure_base(
                    self.compressed_data_dir_url,
                    # NOTE: hex alpha-digits and dot(.) are not special character
                    #       so no need to use quote here.
                    f"{_digest_str}.{_compress_alg}",
                ),
                dst=self._copy_dst / _digest_str,
                original_size=resource.original_size,
                digest=_digest_str,
                digest_alg=DIGEST_ALG,
                compression_alg=_compress_alg,
            )

        # v1 OTA image, uncompressed and use full path as URL path
        # example: http://example.com/base_url/data/rootfs/full/path/file
        assert (_rs_fpath := resource.path), f"invalid {resource=}"
        _relative_rs_fpath = os.path.relpath(_rs_fpath, "/")

        return DownloadInfo(
            url=urljoin_ensure_base(self.data_dir_url, quote(_relative_rs_fpath)),
            dst=self._copy_dst / _digest_str,
            original_size=resource.original_size,
            digest=_digest_str,
            digest_alg=DIGEST_ALG,
        )

    def get_download_list(
        self, *, batch_size: int
    ) -> Generator[DownloadInfo, None, None]:
        """Iter through the resource table and yield DownloadInfo for every resource."""
        _conn = self._ota_metadata.connect_rstable()
        _orm = RSTORM(_conn)
        try:
            for entry in _orm.iter_all_with_shuffle(batch_size=batch_size):
                yield self.get_download_info(entry)
        finally:
            _conn.close()
