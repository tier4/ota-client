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
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Generator
from urllib.parse import quote

from simple_sqlite3_orm.utils import (
    enable_mmap,
    enable_tmp_store_at_memory,
    enable_wal_mode,
)

from ota_metadata.file_table import (
    FileTableNonRegularFilesORM,
    FileTableRegularFilesORM,
)
from ota_metadata.file_table._table import FileTableRegularFiles
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
from .rs_table import ResourceTable, ResourceTableORM

logger = logging.getLogger(__name__)


def _sort_ft_regular_in_place(_orm: FileTableRegularFilesORM) -> None:
    """Sort the ft_regular table by digest, and then replace the old table
    with the sorted one.

    This is required for later otaclient applies update to standby slot.
    """
    SORTED_TABLE_NAME = "ft_regular_sorted"
    ORIGINAL_TABLE_NAME = FileTableRegularFiles.table_name

    _table_create_stmt = _orm.orm_table_spec.table_create_stmt(SORTED_TABLE_NAME)
    _dump_sorted = (
        f"INSERT INTO {SORTED_TABLE_NAME} SELECT * FROM "
        f"{ORIGINAL_TABLE_NAME} ORDER BY digest;"
    )

    conn = _orm.orm_con
    with conn as conn:
        conn.executescript(
            "\n".join(
                [
                    "BEGIN",
                    _table_create_stmt,
                    _dump_sorted,
                    f"DROP TABLE {ORIGINAL_TABLE_NAME};",
                    f"ALTER TABLE {SORTED_TABLE_NAME} RENAME TO {ORIGINAL_TABLE_NAME};",
                ]
            )
        )

    with conn as conn:
        conn.execute("VACUUM;")


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

        self._download_tmp = download_tmp = TemporaryDirectory(
            dir=work_dir, prefix=".download", suffix=os.urandom(8).hex()
        )
        self._download_folder = Path(download_tmp.name)

        self._metadata_jwt = None
        self._total_regulars_num = 0

    @property
    def metadata_jwt(self) -> _MetadataJWTClaimsLayout | None:
        return self._metadata_jwt

    @property
    def total_regulars_num(self) -> int:
        return self._total_regulars_num

    def download_metafiles(self) -> Generator[DownloadInfo, None, None]:
        try:
            # ------ step 1: download metadata.jwt ------ #
            _metadata_jwt_fpath = self._download_folder / self.ENTRY_POINT
            yield DownloadInfo(
                url=urljoin_ensure_base(self._base_url, self.ENTRY_POINT),
                dst=str(_metadata_jwt_fpath),
            )

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
            yield DownloadInfo(
                url=urljoin_ensure_base(self._base_url, cert_fname),
                dst=str(_cert_fpath),
                digest_alg=self.DIGEST_ALG,
                digest=cert_hash,
            )

            cert_bytes = _cert_fpath.read_bytes()
            _parser.verify_metadata_cert(cert_bytes)
            _parser.verify_metadata_signature(cert_bytes)

            # only after the verification, assign the jwt to self
            self._metadata_jwt = _metadata_jwt

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
            enable_mmap(_ft_db_conn)
            enable_wal_mode(_ft_db_conn)
            enable_tmp_store_at_memory(_ft_db_conn)

            _rs_db_conn = sqlite3.connect(self._work_dir / self.RSTABLE_DB)
            enable_mmap(_rs_db_conn)
            enable_wal_mode(_rs_db_conn)
            enable_tmp_store_at_memory(_rs_db_conn)

            _ft_regular_orm = FileTableRegularFilesORM(_ft_db_conn)
            _ft_non_regular_orm = FileTableNonRegularFilesORM(_ft_db_conn)
            _ft_regular_orm.orm_create_table()
            _ft_non_regular_orm.orm_create_table()

            _rs_orm = ResourceTableORM(_rs_db_conn)
            _rs_orm.orm_create_table()

            try:
                parse_dirs_from_csv_file(
                    str(self._download_folder / _metadata_jwt.directory.file),
                    _ft_non_regular_orm,
                )
                parse_symlinks_from_csv_file(
                    str(self._download_folder / _metadata_jwt.symboliclink.file),
                    _ft_non_regular_orm,
                )

                self._total_regulars_num = parse_regulars_from_csv_file(
                    str(self._download_folder / _metadata_jwt.regular.file),
                    _orm=_ft_regular_orm,
                    _orm_rs=_rs_orm,
                )
                _sort_ft_regular_in_place(_ft_regular_orm)
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
            self._download_tmp.cleanup()

    def iter_persist_entries(self) -> Generator[str, None, None]:
        _persist_fpath = self._work_dir / self.PERSIST_FNAME
        with open(_persist_fpath, "r") as f:
            for line in f:
                yield line.strip()[1:-1]

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


class ResourceMeta:

    def __init__(
        self,
        *,
        base_url: str,
        ota_metadata: OTAMetadata,
        copy_dst: Path,
    ) -> None:
        _metadata_jwt = ota_metadata.metadata_jwt
        assert _metadata_jwt, "ota_metadata loading is not finished!"

        self.base_url = base_url
        self.data_dir_url = urljoin_ensure_base(
            base_url, _metadata_jwt.rootfs_directory
        )
        self.compressed_data_dir_url = None
        if _compressed_data := _metadata_jwt.compressed_rootfs_directory:
            self.compressed_data_dir_url = urljoin_ensure_base(
                base_url, _compressed_data
            )

        self._rs_orm = ResourceTableORM(ota_metadata.connect_rstable(read_only=True))
        self._copy_dst = copy_dst

        self._download_list_len = self._get_download_list_len()
        self._download_size = self._get_download_size()

    def _get_download_list_len(self) -> int:
        _sql_stmt = ResourceTable.table_select_stmt(
            select_from=ResourceTable.table_name,
            function="count",
        )
        _query = self._rs_orm.orm_con.execute(_sql_stmt)
        _download_list_len = _query.fetchone()
        assert isinstance(_download_list_len, int)
        return _download_list_len

    def _get_download_size(self):
        _sql_stmt = ResourceTable.table_select_stmt(
            select_from=ResourceTable.table_name,
            select_cols=("size",),
            function="sum",
        )
        _query = self._rs_orm.orm_con.execute(_sql_stmt)
        _download_size = _query.fetchone()
        assert isinstance(_download_size, int)
        return _download_size

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
                dst=str(self._copy_dst / _digest_str),
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
            dst=str(self._copy_dst / _digest_str),
            original_size=resource.original_size,
            digest=_digest_str,
            digest_alg=DIGEST_ALG,
        )

    def get_download_list(
        self, *, batch_size: int
    ) -> Generator[DownloadInfo, None, None]:
        """Iter through the resource table and yield DownloadInfo for every resource."""
        for entry in self._rs_orm.iter_all_with_shuffle(batch_size=batch_size):
            yield self.get_download_info(entry)
