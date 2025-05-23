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
"""Generate delta from delta_src comparing to new OTA image."""


from __future__ import annotations

import logging
import os
import os.path as os_path
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from hashlib import sha256
from pathlib import Path
from queue import Queue

from ota_metadata.file_table._orm import (
    FileTableDirORMPool,
    FileTableRegularORMPool,
)
from ota_metadata.file_table._table import FileTableRegularFiles
from ota_metadata.legacy2.metadata import OTAMetadata
from ota_metadata.legacy2.rs_table import ResourceTableORMPool
from otaclient._status_monitor import StatusReport, UpdateProgressReport
from otaclient.configs.cfg import cfg
from otaclient_common import EMPTY_FILE_SHA256, EMPTY_FILE_SHA256_BYTE
from otaclient_common._logging import BurstSuppressFilter
from otaclient_common._typing import StrOrPath
from otaclient_common.common import create_tmp_fname

logger = logging.getLogger(__name__)
burst_suppressed_logger = logging.getLogger(f"{__name__}.process_file_error")
# NOTE: for request_error, only allow max 6 lines of logging per 30 seconds
burst_suppressed_logger.addFilter(
    BurstSuppressFilter(
        f"{__name__}.process_file_error",
        upper_logger_name=__name__,
        burst_round_length=30,
        burst_max=6,
    )
)

CANONICAL_ROOT = cfg.CANONICAL_ROOT

SHA256HEXSTRINGLEN = 256 // 8 * 2
DELETE_BATCH_SIZE = 512
DB_CONN_NUMS = 2


class DeltaGenerator:
    """
    NOTE: the instance of this class cannot be re-used after delta is generated.
    """

    def __init__(
        self,
        *,
        ota_metadata: OTAMetadata,
        delta_src: Path,
        copy_dst: Path,
        status_report_queue: Queue[StatusReport],
        session_id: str,
    ) -> None:
        self._ota_metadata = ota_metadata
        self._status_report_queue = status_report_queue
        self.session_id = session_id

        self._delta_src_mount_point = delta_src
        self._copy_dst = copy_dst

        self._ft_regular_orm = FileTableRegularORMPool(
            con_factory=ota_metadata.connect_fstable,
            number_of_cons=DB_CONN_NUMS,
            thread_name_prefix="ft_reg_orm_pool",
        )
        self._rst_orm_pool = ResourceTableORMPool(
            con_factory=ota_metadata.connect_rstable,
            number_of_cons=DB_CONN_NUMS,
            thread_name_prefix="ota_delta_gen",
        )

        self._max_pending_tasks = threading.Semaphore(
            cfg.MAX_CONCURRENT_PROCESS_FILE_TASKS
        )

        # put the empty file into copy_dst
        (copy_dst / EMPTY_FILE_SHA256).touch()

    @staticmethod
    def _thread_worker_initializer(thread_local) -> None:
        thread_local.buffer = buffer = bytearray(cfg.CHUNK_SIZE)
        thread_local.view = memoryview(buffer)


class DeltaGenWithFileTable(DeltaGenerator):

    def _process_file(
        self, _input: tuple[bytes, list[FileTableRegularFiles]], thread_local
    ) -> None:
        expected_digest, entries = _input
        dst_f = self._copy_dst / expected_digest.hex()
        if expected_digest == EMPTY_FILE_SHA256_BYTE:
            return

        src_dir = self._delta_src_mount_point
        for entry in entries:
            src_at_mntp = src_dir / os_path.relpath(entry.path, "/")
            if not src_at_mntp.is_file():
                continue

            tmp_f = self._copy_dst / create_tmp_fname()
            try:
                hash_buffer, hash_bufferview = (thread_local.buffer, thread_local.view)
                hash_f = sha256()
                with open(src_at_mntp, "rb") as src, open(tmp_f, "wb") as tmp_dst:
                    while read_size := src.readinto(hash_buffer):
                        hash_f.update(hash_bufferview[:read_size])
                        tmp_dst.write(hash_bufferview[:read_size])
                if hash_f.digest() != entry.digest:
                    continue

                if self._rst_orm_pool.orm_delete_entries(digest=hash_f.digest()) == 1:
                    tmp_f.rename(dst_f)  # rename will unconditionally replace the dst_f
                    self._status_report_queue.put_nowait(
                        StatusReport(
                            payload=UpdateProgressReport(
                                operation=UpdateProgressReport.Type.PREPARE_LOCAL_COPY,
                                processed_file_size=entry.entry_attrs.size or 0,
                                processed_file_num=1,
                            ),
                            session_id=self.session_id,
                        )
                    )
                    # return on successfully prepare the resource
                    return
            finally:
                tmp_f.unlink(missing_ok=True)

    # API

    def calculate_delta(self, *, base_file_table: StrOrPath) -> None:
        logger.debug("process delta src, generate delta and prepare local copy...")
        thread_local = threading.local()
        se = self._max_pending_tasks

        def _release_se(_):
            se.release()

        pool = ThreadPoolExecutor(
            max_workers=cfg.MAX_PROCESS_FILE_THREAD,
            thread_name_prefix="scan_slot",
            initializer=partial(self._thread_worker_initializer, thread_local),
        )
        try:
            for item in self._ota_metadata.iter_common_regular_entries_by_digest(
                base_file_table=base_file_table
            ):
                self._max_pending_tasks.acquire()
                pool.submit(
                    self._process_file, item, thread_local=thread_local
                ).add_done_callback(_release_se)
            # heal the holes of rs_table
            self._rst_orm_pool.orm_execute("VACUUM;")
        finally:
            pool.shutdown(wait=True)
            self._ft_regular_orm.orm_pool_shutdown()
            self._rst_orm_pool.orm_pool_shutdown()


class DeltaGenFullDiskScan(DeltaGenerator):
    # entry under the following folders will be scanned
    # no matter it is existed in new image or not
    FULL_SCAN_PATHS = {
        "/lib",
        "/var/lib",
        "/usr",
        "/opt/nvidia",
        "/home/autoware/autoware.proj",
    }

    # entries start with the following paths will be ignored
    EXCLUDE_PATHS = {
        "/tmp",
        "/dev",
        "/proc",
        "/sys",
        "/lost+found",
        "/media",
        "/mnt",
        "/run",
        "/srv",
    }

    # introduce limitations here to prevent unexpected
    # scanning in unknown large, deep folders in full
    # scan mode.
    # NOTE: the following settings are enough for most cases
    MAX_FOLDER_DEEPTH = 23
    MAX_FILENUM_PER_FOLDER = 8192

    def _process_file(
        self,
        fpath: Path,
        canonical_fpath: Path,
        *,
        fully_scan: bool,
        thread_local,
    ) -> None:
        try:
            if not fpath.is_file() or fpath.stat().st_size == 0:
                return  # skip empty file

            # in default match_only mode, if the fpath doesn't exist in new, ignore
            if not fully_scan and not self._ft_regular_orm.orm_check_entry_exist(
                path=str(canonical_fpath)
            ):
                return

            tmp_f = self._copy_dst / create_tmp_fname()
            try:
                hash_buffer, hash_bufferview = thread_local.buffer, thread_local.view

                hash_f = sha256()
                with open(fpath, "rb") as src, open(tmp_f, "wb") as tmp_dst:
                    while read_size := src.readinto(hash_buffer):
                        hash_f.update(hash_bufferview[:read_size])
                        tmp_dst.write(hash_bufferview[:read_size])

                dst_f = self._copy_dst / hash_f.hexdigest()

                # If the resource we scan here is listed in the resouce table, copy it
                #   to the copy_dir at standby slot for later use.
                if self._rst_orm_pool.orm_delete_entries(digest=hash_f.digest()) == 1:
                    tmp_f.rename(dst_f)  # rename will unconditionally replace the dst_f
                    self._status_report_queue.put_nowait(
                        StatusReport(
                            payload=UpdateProgressReport(
                                operation=UpdateProgressReport.Type.PREPARE_LOCAL_COPY,
                                processed_file_size=fpath.stat().st_size,
                                processed_file_num=1,
                            ),
                            session_id=self.session_id,
                        )
                    )
            finally:
                tmp_f.unlink(missing_ok=True)
        except Exception as e:
            burst_suppressed_logger.warning(f"failed to proces {fpath}: {e!r}")
        finally:
            self._max_pending_tasks.release()  # always release se first

    def calculate_delta(self) -> None:  # NOSONAR
        logger.debug("process delta src, generate delta and prepare local copy...")
        _canonical_root = Path(CANONICAL_ROOT)

        thread_local = threading.local()

        ft_dir_orm = FileTableDirORMPool(
            con_factory=self._ota_metadata.connect_fstable,
            number_of_cons=DB_CONN_NUMS,
            thread_name_prefix="ft_dir_orm_pool",
        )

        pool = ThreadPoolExecutor(
            max_workers=cfg.MAX_PROCESS_FILE_THREAD,
            thread_name_prefix="scan_slot",
            initializer=partial(self._thread_worker_initializer, thread_local),
        )
        try:
            # scan old slot and generate delta based on path,
            # group files into many hash group,
            # each hash group is a set contains RegularInf(s) with path as key.
            #
            # if the scanned file's hash existed in _new,
            # collect this file to the recycle folder if not yet being collected.
            for curdir, dirnames, filenames in os.walk(
                self._delta_src_mount_point, topdown=True, followlinks=False
            ):
                delta_src_curdir_path = Path(curdir)
                canonical_curdir_path = (
                    _canonical_root
                    / delta_src_curdir_path.relative_to(self._delta_src_mount_point)
                )

                if len(canonical_curdir_path.parents) > self.MAX_FOLDER_DEEPTH:
                    logger.warning(
                        f"{canonical_curdir_path=} exceeds {self.MAX_FOLDER_DEEPTH=}, skip scan this folder"
                    )
                    dirnames.clear()
                    continue

                dir_should_fully_scan = False
                dir_is_excluded = False
                for parent in reversed(canonical_curdir_path.parents):
                    _cur_parent = str(parent)
                    if _cur_parent in self.FULL_SCAN_PATHS:
                        dir_should_fully_scan = True
                        break
                    elif _cur_parent in self.EXCLUDE_PATHS:
                        dir_is_excluded = True
                        break

                if dir_is_excluded:
                    dirnames.clear()
                    continue

                # If current dir is not:
                #   1. the root folder
                #   2. should fully scan folder
                #   3. folder existed in the new OTA image
                # skip scanning this folder and its subfolders.
                _str_canon_fpath = str(canonical_curdir_path)
                if (
                    _str_canon_fpath != CANONICAL_ROOT
                    and not dir_should_fully_scan
                    and not ft_dir_orm.orm_check_entry_exist(path=_str_canon_fpath)
                ):
                    dirnames.clear()
                    continue

                # skip files that over the max_filenum_per_folder,
                # and add these files to remove list
                if len(filenames) > self.MAX_FILENUM_PER_FOLDER:
                    logger.warning(
                        f"reach max_filenum_per_folder on {delta_src_curdir_path}, "
                        "exceeded files will be ignored silently"
                    )

                for fname in filenames[: self.MAX_FILENUM_PER_FOLDER]:
                    delta_src_fpath = delta_src_curdir_path / fname

                    # ignore non-file file(include symlink)
                    # NOTE: for in-place update, we will recreate all the symlinks,
                    #       so we first remove all the symlinks
                    # NOTE: is_file also return True on symlink points to regular file!
                    if delta_src_fpath.is_symlink() or not delta_src_fpath.is_file():
                        continue

                    self._max_pending_tasks.acquire()
                    pool.submit(
                        self._process_file,
                        delta_src_fpath,
                        # NOTE: ALWAYS use canonical_fpath in db search!
                        canonical_curdir_path / fname,
                        fully_scan=dir_should_fully_scan,
                        thread_local=thread_local,
                    )

            # heals the hole of the rs table
            self._rst_orm_pool.orm_execute("VACUUM;")
        finally:
            pool.shutdown(wait=True)
            self._ft_regular_orm.orm_pool_shutdown()
            ft_dir_orm.orm_pool_shutdown()
            self._rst_orm_pool.orm_pool_shutdown()
