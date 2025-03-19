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

Version1 OTA metafiles list:
- directory: all directories in the image,
- symboliclink: all symlinks in the image,
- regular: all normal/regular files in the image,
- persistent: files that should be preserved across update.

"""


from __future__ import annotations

import contextlib
import logging
import os.path
import shutil
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Generator
from urllib.parse import quote

from simple_sqlite3_orm import utils
from simple_sqlite3_orm.utils import (
    enable_tmp_store_at_memory,
    enable_wal_mode,
    sort_and_replace,
)

from ota_metadata.file_table import (
    FileTableDirectories,
    FileTableNonRegularFiles,
    FileTableRegularFiles,
)
from ota_metadata.file_table._orm import (
    FileTableDirORM,
    FileTableNonRegularORM,
    FileTableRegularORM,
)
from ota_metadata.utils.cert_store import CAChainStore
from otaclient.configs.cfg import cfg
from otaclient_common._typing import StrOrPath
from otaclient_common.common import urljoin_ensure_base
from otaclient_common.download_info import DownloadInfo

from . import DIGEST_ALG, SUPORTED_COMPRESSION_TYPES
from .csv_parser import (
    parse_dirs_from_csv_file,
    parse_regulars_from_csv_file,
    parse_symlinks_from_csv_file,
)
from .metadata_jwt import (
    MetadataJWTClaimsLayout,
    MetadataJWTParser,
    MetadataJWTVerificationFailed,
)
from .rs_table import ResourceTable, ResourceTableORM

logger = logging.getLogger(__name__)

# NOTE: enlarge the connection timeout on waiting db lock.
DB_TIMEOUT = 16  # seconds

MAX_ENTRIES_PER_DIGEST = 10
"""How many entries to scan through for each unique digest."""


def check_base_filetable(db_f: StrOrPath | None) -> StrOrPath | None:
    if not db_f or not Path(db_f).is_file():
        return

    with contextlib.suppress(Exception), contextlib.closing(
        sqlite3.connect(f"file:{db_f}?mode=ro&immutable=1", uri=True)
    ) as con:
        if utils.check_db_integrity(
            con,
            table_name=FileTableRegularORM.orm_bootstrap_table_name,
        ):
            return db_f


class OTAMetadata:
    """
    OTA session_dir layout:
    session_<session_id> /
        - / .download_<random> # the download area for OTA image files
        - / file_table.sqlite3 # the file table generated from metafiles,
                            # this will be saved to standby slot.
        - / resource_table.sqlite3 # the resource table generated from metafiles.
        - / persists.txt # the persist files list.

    """

    ENTRY_POINT = "metadata.jwt"
    DIGEST_ALG = "sha256"
    FSTABLE_DB = "file_table.sqlite3"
    RSTABLE_DB = "resource_table.sqlite3"
    PERSIST_META_FNAME = "persists.txt"

    def __init__(
        self,
        *,
        base_url: str,
        session_dir: StrOrPath,
        ca_chains_store: CAChainStore,
    ) -> None:
        if not ca_chains_store:
            _err_msg = "CA chains store is empty!!! immediately fail the verification"
            logger.error(_err_msg)
            raise MetadataJWTVerificationFailed(_err_msg)

        self._ca_store = ca_chains_store
        self._base_url = base_url

        self._session_dir = Path(session_dir)
        self._fst_db = self._session_dir / self.FSTABLE_DB
        self._rst_db = self._session_dir / self.RSTABLE_DB

        self._metadata_jwt = None
        self._total_regulars_num = 0

    @property
    def metadata_jwt(self) -> MetadataJWTClaimsLayout:
        assert self._metadata_jwt, "metadata_jwt is not ready yet!"
        return self._metadata_jwt

    @property
    def total_regulars_num(self) -> int:
        return self._total_regulars_num

    def _prepare_metadata(
        self,
        _download_dir: Path,
        condition: threading.Condition,
    ) -> Generator[list[DownloadInfo]]:
        """Download raw metadata.jwt, parse and verify it.

        After processing is finished, assigned the parsed metadata to inst.
        """

        # ------ step 1: download metadata.jwt ------ #
        _metadata_jwt_fpath = _download_dir / self.ENTRY_POINT
        with condition:
            yield [
                DownloadInfo(
                    url=urljoin_ensure_base(self._base_url, self.ENTRY_POINT),
                    dst=_metadata_jwt_fpath,
                )
            ]
            condition.wait(cfg.DOWNLOAD_INACTIVE_TIMEOUT)  # wait for download finished

        _parser = MetadataJWTParser(
            _metadata_jwt_fpath.read_text(),
            ca_chains_store=self._ca_store,
        )

        # get not yet verified parsed ota_metadata
        _metadata_jwt = _parser.metadata_jwt
        _metadata_jwt_fpath.unlink(missing_ok=True)

        # ------ step 2: download the certificate itself ------ #
        cert_info = _metadata_jwt.certificate
        cert_fname, cert_hash = cert_info.file, cert_info.hash

        _cert_fpath = _download_dir / cert_fname
        with condition:
            yield [
                DownloadInfo(
                    url=urljoin_ensure_base(self._base_url, cert_fname),
                    dst=_cert_fpath,
                    digest_alg=self.DIGEST_ALG,
                    digest=cert_hash,
                )
            ]
            condition.wait(cfg.DOWNLOAD_INACTIVE_TIMEOUT)

        cert_bytes = _cert_fpath.read_bytes()
        _parser.verify_metadata_cert(cert_bytes)
        _parser.verify_metadata_signature(cert_bytes)

        # only after the verification, assign the jwt to self
        self._metadata_jwt = _metadata_jwt
        _cert_fpath.unlink(missing_ok=True)

    def _prepare_ota_image_metadata(
        self, _download_dir: Path, condition: threading.Condition
    ) -> Generator[list[DownloadInfo]]:
        """Download filetable related OTA image metadata files.

        Including:
            1. regular
            2. directory
            3. symboliclink
        """
        metadata_jwt = self.metadata_jwt

        # ------ setup database ------ #
        with closing(self.connect_fstable()) as fst_conn, closing(
            self.connect_rstable()
        ) as rst_conn:
            ft_regular_orm = FileTableRegularORM(fst_conn)
            ft_regular_orm.orm_bootstrap_db()
            ft_dir_orm = FileTableDirORM(fst_conn)
            ft_dir_orm.orm_bootstrap_db()
            ft_non_regular_orm = FileTableNonRegularORM(fst_conn)
            ft_non_regular_orm.orm_bootstrap_db()

            rs_orm = ResourceTableORM(rst_conn)
            rs_orm.orm_bootstrap_db()

            # ------ download metafiles ------ #
            regular_meta = metadata_jwt.regular
            regular_download_url = urljoin_ensure_base(
                self._base_url, regular_meta.file
            )
            regular_save_fpath = _download_dir / regular_meta.file

            dir_meta = metadata_jwt.directory
            dir_download_url = urljoin_ensure_base(self._base_url, dir_meta.file)
            dir_save_fpath = _download_dir / dir_meta.file

            symlink_meta = metadata_jwt.symboliclink
            symlink_download_url = urljoin_ensure_base(
                self._base_url, symlink_meta.file
            )
            symlink_save_fpath = _download_dir / symlink_meta.file

            _download_list = [
                DownloadInfo(
                    url=regular_download_url,
                    dst=regular_save_fpath,
                    digest_alg=self.DIGEST_ALG,
                    digest=regular_meta.hash,
                ),
                DownloadInfo(
                    url=dir_download_url,
                    dst=dir_save_fpath,
                    digest_alg=self.DIGEST_ALG,
                    digest=dir_meta.hash,
                ),
                DownloadInfo(
                    url=symlink_download_url,
                    dst=symlink_save_fpath,
                    digest_alg=self.DIGEST_ALG,
                    digest=symlink_meta.hash,
                ),
            ]
            with condition:
                yield _download_list
                condition.wait(cfg.DOWNLOAD_INACTIVE_TIMEOUT)  # wait for download finished

            # ------ parse metafiles ------ #
            self._total_regulars_num = regulars_num = parse_regulars_from_csv_file(
                _fpath=regular_save_fpath,
                _orm=ft_regular_orm,
                _orm_rs=rs_orm,
            )
            regular_save_fpath.unlink(missing_ok=True)

            dirs_num = parse_dirs_from_csv_file(dir_save_fpath, ft_dir_orm)
            dir_save_fpath.unlink(missing_ok=True)

            symlinks_num = parse_symlinks_from_csv_file(
                symlink_save_fpath, ft_non_regular_orm
            )
            symlink_save_fpath.unlink(missing_ok=True)

        logger.info(
            f"csv parse finished: {dirs_num=}, {symlinks_num=}, {regulars_num=}"
        )

    def _prepare_persist_meta(
        self, _download_dir: Path, condition: threading.Condition
    ) -> Generator[list[DownloadInfo]]:
        persist_meta = self.metadata_jwt.persistent

        persist_meta_download_url = urljoin_ensure_base(
            self._base_url, persist_meta.file
        )
        persist_meta_save_fpath = _download_dir / self.PERSIST_META_FNAME
        with condition:
            yield [
                DownloadInfo(
                    url=persist_meta_download_url,
                    dst=persist_meta_save_fpath,
                    digest_alg=self.DIGEST_ALG,
                    digest=persist_meta.hash,
                )
            ]
            condition.wait(cfg.DOWNLOAD_INACTIVE_TIMEOUT)

        # save the persists.txt to session_dir for later use
        shutil.move(str(persist_meta_save_fpath), self._session_dir)

    # APIs

    def download_metafiles(
        self,
        condition: threading.Condition,
    ) -> Generator[list[DownloadInfo]]:
        """Guide the caller to download metadata files by yielding the DownloadInfo instances.

        While the caller downloading the metadata files one by one, this method will:
        1. download, parse and verify metadata.jwt.
        2. download and parse OTA image metadata files into database.
        3. download persists.txt.
        """
        _download_dir = df = self._session_dir / f".download_{os.urandom(4).hex()}"
        df.mkdir(exist_ok=True, parents=True)

        try:
            yield from self._prepare_metadata(_download_dir, condition)
            yield from self._prepare_ota_image_metadata(_download_dir, condition)
            yield from self._prepare_persist_meta(_download_dir, condition)
        except Exception as e:
            logger.exception(
                f"failure during downloading and verifying OTA image metafiles: {e!r}"
            )
            raise
        finally:
            shutil.rmtree(_download_dir, ignore_errors=True)

    def save_fstable(self, dst: StrOrPath, db_fname: str = FSTABLE_DB) -> None:
        """Dump the file_table to <dst>/<db_fname>"""
        with closing(self.connect_fstable()) as _fs_conn, closing(
            sqlite3.connect(Path(dst) / db_fname)
        ) as _dst_conn:
            with _dst_conn as conn:
                _fs_conn.backup(conn)

            with _dst_conn as conn:
                conn.execute("VACUUM;")
                # change the journal_mode back to DELETE to make db file on read-only mount work.
                # see https://www.sqlite.org/wal.html#read_only_databases for more details.
                conn.execute("PRAGMA journal_mode=DELETE;")

    def prepare_fstable(self) -> None:
        """Optimize the file_table to be ready for delta generation use."""
        _orm = FileTableRegularORM(self.connect_fstable)
        sort_and_replace(
            _orm,
            table_name=_orm.orm_table_name,
            order_by_col="digest",
        )

    # helper methods

    def iter_persist_entries(self) -> Generator[str]:
        with open(self._session_dir / self.PERSIST_META_FNAME, "r") as f:
            for line in f:
                yield line.strip()[1:-1]

    def iter_dir_entries(self, *, batch_size: int) -> Generator[FileTableDirectories]:
        with FileTableDirORM(self.connect_fstable()) as orm:
            yield from orm.orm_select_all_with_pagination(batch_size=batch_size)

    def iter_non_regular_entries(
        self, *, batch_size: int
    ) -> Generator[FileTableNonRegularFiles]:
        """Yield entries from base file_table which digest presented in OTA image's file_table.

        This is for assisting faster delta_calculation without full disk scan.
        """
        with FileTableNonRegularORM(self.connect_fstable()) as orm:
            yield from orm.orm_select_all_with_pagination(batch_size=batch_size)

    def iter_common_regular_entries_by_digest(
        self,
        base_file_table: StrOrPath,
        *,
        max_num_of_entries_per_digest: int = MAX_ENTRIES_PER_DIGEST,
    ) -> Generator[tuple[bytes, list[FileTableRegularFiles]]]:
        _hash, _cur = b"", []
        with FileTableRegularORM(self.connect_fstable()) as orm:
            for entry in orm.iter_common_by_digest(str(base_file_table)):
                if entry.digest == _hash:
                    # When there are too many entries for this digest, just pick the first
                    #   <max_num_of_entries_per_digest> of them.
                    if len(_cur) <= max_num_of_entries_per_digest:
                        _cur.append(entry)
                else:
                    if _cur:
                        yield _hash, _cur
                    _hash, _cur = entry.digest, [entry]

            if _cur:
                yield _hash, _cur

    def iter_regular_entries(
        self, *, batch_size: int
    ) -> Generator[FileTableRegularFiles]:
        # NOTE: do the dispatch at a thread
        with FileTableRegularORM(self.connect_fstable()) as orm:
            yield from orm.orm_select_all_with_pagination(batch_size=batch_size)

    def connect_fstable(self) -> sqlite3.Connection:
        _conn = sqlite3.connect(
            self._fst_db, check_same_thread=False, timeout=DB_TIMEOUT
        )
        enable_wal_mode(_conn)
        enable_tmp_store_at_memory(_conn)
        return _conn

    def connect_rstable(self) -> sqlite3.Connection:
        _conn = sqlite3.connect(
            self._rst_db, check_same_thread=False, timeout=DB_TIMEOUT
        )
        enable_wal_mode(_conn)
        enable_tmp_store_at_memory(_conn)
        return _conn


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

    @property
    def resources_count(self) -> int:
        _conn = self._ota_metadata.connect_rstable()
        _orm = ResourceTableORM(_conn, row_factory=None)
        _sql_stmt = ResourceTable.table_select_stmt(
            select_from=_orm.orm_table_name,
            function="count",
        )

        try:
            _query = _orm.orm_execute(_sql_stmt)
            # NOTE: return value of fetchone will be a tuple, and here
            #   the first and only value of the tuple is the total nums of entries.
            assert _query  # should be something like ((<int>,),)
            assert isinstance(res := _query[0][0], int)
            return res
        except Exception as e:
            logger.warning(f"failed to get resources_count: {e!r}")
            return 0
        finally:
            _conn.close()

    @property
    def resources_size_sum(self) -> int:
        _conn = self._ota_metadata.connect_rstable()
        _orm = ResourceTableORM(_conn, row_factory=None)

        _sql_stmt = ResourceTable.table_select_stmt(
            select_from=_orm.orm_table_name,
            select_cols=("original_size",),
            function="sum",
        )

        try:
            _query = _orm.orm_execute(_sql_stmt)
            # NOTE: return value of fetchone will be a tuple, and here
            #   the first and only value of the tuple is the total nums of entries.
            assert _query  # should be something like ((<int>,),)
            assert isinstance(res := _query[0][0], int)
            return res
        except Exception as e:
            logger.warning(f"failed to get resources_size_sum: {e!r}")
            return 0
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

    def iter_resources(self, *, batch_size: int) -> Generator[DownloadInfo]:
        """Iter through the resource table and yield DownloadInfo for every resource."""
        with ResourceTableORM(self._ota_metadata.connect_rstable()) as orm:
            for entry in orm.iter_all_with_shuffle(batch_size=batch_size):
                yield self.get_download_info(entry)
