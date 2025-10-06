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

import logging
import os.path
import shutil
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Generator
from urllib.parse import quote

from ota_image_libs.v1.file_table.db import (
    FileTableDBHelper,
    FileTableDirORM,
    FileTableInodeORM,
    FileTableNonRegularORM,
    FileTableRegularORM,
    FileTableResourceORM,
)

from ota_metadata.errors import NoCAStoreAvailable
from ota_metadata.utils.cert_store import CAChainStore
from otaclient_common._typing import StrOrPath
from otaclient_common.common import urljoin_ensure_base
from otaclient_common.download_info import DownloadInfo

from . import DIGEST_ALG, SUPORTED_COMPRESSION_TYPES
from .csv_parser import (
    parse_dirs_from_csv_file,
    parse_regulars_from_csv_file,
    parse_symlinks_from_csv_file,
)
from .metadata_jwt import MetadataJWTClaimsLayout, MetadataJWTParser
from .rs_table import ResourceTableORM, ResourceTableORMPool

logger = logging.getLogger(__name__)

# NOTE: enlarge the connection timeout on waiting db lock.
DB_TIMEOUT = 16  # seconds

MAX_ENTRIES_PER_DIGEST = 10
"""How many entries to scan through for each unique digest."""


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
            raise NoCAStoreAvailable(_err_msg)

        self._ca_store = ca_chains_store
        self._base_url = base_url

        self._session_dir = Path(session_dir)
        self._fst_db = self._session_dir / self.FSTABLE_DB
        self._rst_db = self._session_dir / self.RSTABLE_DB

        self._metadata_jwt = None
        self._total_regulars_num = 0
        self._total_dirs_num = 0
        self._total_symlinks_num = 0
        self._total_regulars_size = 0

    @property
    def metadata_jwt(self) -> MetadataJWTClaimsLayout:
        if not self._metadata_jwt:
            raise ValueError("metadata_jwt is not ready yet!")
        return self._metadata_jwt

    @property
    def total_regulars_num(self) -> int:
        return self._total_regulars_num

    @property
    def total_dirs_num(self) -> int:
        return self._total_dirs_num

    @property
    def total_symlinks_num(self) -> int:
        return self._total_symlinks_num

    @property
    def total_regulars_size(self) -> int:
        return self._total_regulars_size

    @property
    def file_table_helper(self) -> FileTableDBHelper:
        return FileTableDBHelper(self._fst_db)

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
            condition.wait()  # wait for download finished

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
            condition.wait()

        cert_bytes = _cert_fpath.read_bytes()
        _parser.verify_metadata_cert(cert_bytes)
        _parser.verify_metadata_signature(cert_bytes)

        # only after the verification, assign the jwt to self
        self._total_regulars_size = _metadata_jwt.total_regular_size
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
            # ------ bootstrap each tables in the file_table database ------ #
            ft_regular_orm = FileTableRegularORM(fst_conn)
            ft_regular_orm.orm_bootstrap_db()
            ft_dir_orm = FileTableDirORM(fst_conn)
            ft_dir_orm.orm_bootstrap_db()
            ft_non_regular_orm = FileTableNonRegularORM(fst_conn)
            ft_non_regular_orm.orm_bootstrap_db()
            ft_resource_orm = FileTableResourceORM(fst_conn)
            ft_resource_orm.orm_bootstrap_db()
            ft_inode_orm = FileTableInodeORM(fst_conn)
            ft_inode_orm.orm_bootstrap_db()

            # ------ bootstrap table in the resource table database ------ #
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
                condition.wait()  # wait for download finished

            inode_start = 1
            # ------ parse metafiles ------ #
            regulars_num, inode_start = parse_regulars_from_csv_file(
                _fpath=regular_save_fpath,
                _orm=ft_regular_orm,
                _orm_ft_resource=ft_resource_orm,
                _orm_rs=rs_orm,
                _orm_inode=ft_inode_orm,
                inode_start=inode_start,
            )
            self._total_regulars_num = regulars_num
            regular_save_fpath.unlink(missing_ok=True)

            dirs_num, inode_start = parse_dirs_from_csv_file(
                dir_save_fpath,
                ft_dir_orm,
                _inode_orm=ft_inode_orm,
                inode_start=inode_start,
            )
            dir_save_fpath.unlink(missing_ok=True)

            symlinks_num, _ = parse_symlinks_from_csv_file(
                symlink_save_fpath,
                ft_non_regular_orm,
                _inode_orm=ft_inode_orm,
                inode_start=inode_start,
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
            condition.wait()

        # save the persists.txt to session_dir for later use
        shutil.move(str(persist_meta_save_fpath), self._session_dir)

    # APIs

    def download_metafiles(
        self,
        condition: threading.Condition,
        *,
        only_metadata_verification: bool = False,
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
            if only_metadata_verification:
                # if only verification is requested, skip the rest of the steps.
                return
            yield from self._prepare_ota_image_metadata(_download_dir, condition)
            yield from self._prepare_persist_meta(_download_dir, condition)
        except Exception as e:
            logger.exception(
                f"failure during downloading and verifying OTA image metafiles: {e!r}"
            )
            raise
        finally:
            shutil.rmtree(_download_dir, ignore_errors=True)

    # helper methods

    def iter_persist_entries(self) -> Generator[str]:
        with open(self._session_dir / self.PERSIST_META_FNAME, "r") as f:
            for line in f:
                yield line.strip()[1:-1]

    def connect_fstable(self) -> sqlite3.Connection:
        _conn = sqlite3.connect(
            self._fst_db, check_same_thread=False, timeout=DB_TIMEOUT
        )
        return _conn

    def connect_rstable(self) -> sqlite3.Connection:
        _conn = sqlite3.connect(
            self._rst_db, check_same_thread=False, timeout=DB_TIMEOUT
        )
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
        self._rst_orm_pool = ResourceTableORMPool(
            con_factory=ota_metadata.connect_rstable,
            number_of_cons=2,
        )

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

    def shutdown(self):
        self._rst_orm_pool.orm_pool_shutdown()

    # API

    def get_download_info(self, digest: bytes) -> DownloadInfo:
        """Get DownloadInfo from one ResourceTable entry.

        This method is save for multi-thread use.

        Returns:
            An instance of DownloadInfo to download the resource indicates by <resource>.
        """
        resource = self._rst_orm_pool.orm_select_entry(digest=digest)
        _digest_str = resource.digest.hex()

        # Legacy OTA image revision 1, with compression enabled
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

        # Legacy OTA image, uncompressed and use full path as URL path
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
