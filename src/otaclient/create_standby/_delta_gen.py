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
import shutil
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from hashlib import sha256
from pathlib import Path
from queue import Queue
from tempfile import TemporaryDirectory
from typing import Any

from ota_metadata.file_table._orm import FileSystemTableORM
from ota_metadata.legacy.metadata import OTAMetadata
from ota_metadata.legacy.rs_table import (
    RSTableORMThreadPool,
)
from otaclient._status_monitor import StatusReport, UpdateProgressReport
from otaclient.configs.cfg import cfg
from otaclient_common.common import create_tmp_fname

logger = logging.getLogger(__name__)

CANONICAL_ROOT = cfg.CANONICAL_ROOT


class DeltaGenerator:
    """
    NOTE: the instance of this class cannot be re-used after delta is generated.
    """

    # entry under the following folders will be scanned
    # no matter it is existed in new image or not
    FULL_SCAN_PATHS = {
        "/lib",
        "/var/lib",
        "/usr",
        "/opt/nvidia",
        "/home/autoware/autoware.proj",
    }

    # introduce limitations here to prevent unexpected
    # scanning in unknown large, deep folders in full
    # scan mode.
    # NOTE: the following settings are enough for most cases
    MAX_FOLDER_DEEPTH = 20
    MAX_FILENUM_PER_FOLDER = 8192

    RS_TABLE_CONN_NUMS = 3

    def __init__(
        self,
        *,
        ota_metadata: OTAMetadata,
        delta_src: Path,
        work_dir: Path,
        copy_dst: Path,
        status_report_queue: Queue[StatusReport],
        session_id: str,
    ) -> None:
        self._ota_metadata = ota_metadata
        self._status_report_queue = status_report_queue
        self.session_id = session_id

        self._delta_src_mount_point = delta_src
        self._tmp_dir = tmp_d = TemporaryDirectory(
            prefix="ota_tmp", suffix=session_id, dir=work_dir
        )
        self._tmp_work_dir = Path(tmp_d.name)
        self._copy_dst = copy_dst

        # NOTE: file_system look_up is single thread
        self._fstable_orm = FileSystemTableORM(
            ota_metadata.connect_fstable(read_only=True)
        )
        # NOTE: we will update the resource table in-place, the
        #       leftover entries in the db will be the to-be-downloaded resources.
        self._rstable_orm = RSTableORMThreadPool(
            con_factory=partial(ota_metadata.connect_rstable, read_only=False),
            number_of_cons=self.RS_TABLE_CONN_NUMS,
            thread_name_prefix="ota_delta_gen",
        )

        self._max_pending_tasks = threading.Semaphore(
            cfg.MAX_CONCURRENT_PROCESS_FILE_TASKS
        )

    def _process_file(self, fpath: Path, *, thread_local) -> None:
        """Hash(and verify the file) and prepare a copy for it in standby slot.
        This is the task entry being executed by each ONE of the pool worker.

        Params:
            fpath: the path to the file to be verified
            expected_hash: (optional) if we have the information for this file
                (by stored OTA image meta for active slot), use it make the
                verification process faster.

        NOTE: verify the file before copying to the standby slot!
        """
        tmp_f = self._tmp_work_dir / create_tmp_fname()
        hash_buffer, hash_bufferview = thread_local.buffer, thread_local.view
        try:
            hash_f = sha256()
            with open(fpath, "rb") as src, open(tmp_f, "wb") as tmp_dst:
                while read_size := src.readinto(hash_buffer):
                    hash_f.update(hash_bufferview[:read_size])
                    tmp_dst.write(hash_bufferview[:read_size])

            dst_f = self._copy_dst / hash_f.hexdigest()
            # try to remove the corresponding entry from the resource table db,
            #   if succeeds, it means this entry match one resource entry in the table,
            #   so we don't need to download it from remote.
            _deleted_entries = 0
            try:
                _deleted_entries = self._rstable_orm.orm_delete_entries(
                    digest=hash_f.digest()
                )
            except Exception as e:
                logger.warning(f"sql execution failed: {e!r}")

            if isinstance(_deleted_entries, int) and _deleted_entries >= 1:
                shutil.move(str(tmp_f), dst_f)
        finally:
            tmp_f.unlink(missing_ok=True)

        # report to the ota update stats collector
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

    @staticmethod
    def _thread_worker_initializer(thread_local):
        thread_local.buffer = buffer = bytearray(cfg.CHUNK_SIZE)
        thread_local.view = memoryview(buffer)

    def _task_done_callback(self, fut: Future[Any]):
        self._max_pending_tasks.release()  # always release se first
        if exc := fut.exception():
            logger.warning(
                f"detect error during file preparing, still continue: {exc!r}"
            )

    def _check_dir_should_fully_scan(self, dpath: Path) -> bool:
        for parent in reversed(dpath.parents):
            if str(parent) in self.FULL_SCAN_PATHS:
                return True
        return False

    def _check_skip_dir(self, dpath: Path) -> bool:
        dir_should_fully_scan = self._check_dir_should_fully_scan(dpath)
        _dpath = str(dpath)
        return (
            _dpath != CANONICAL_ROOT
            and not dir_should_fully_scan
            and not self._fstable_orm.orm_select_entry(path=_dpath)
        )

    # API

    def calculate_delta(self) -> None:
        logger.debug("process delta src, generate delta and prepare local copy...")
        _canonical_root = Path(CANONICAL_ROOT)

        thread_local = threading.local()

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
                logger.debug(f"{delta_src_curdir_path=}, {canonical_curdir_path=}")

                # skip folder that exceeds max_folder_deepth,
                if len(delta_src_curdir_path.parents) > self.MAX_FOLDER_DEEPTH:
                    logger.warning(
                        f"reach max_folder_deepth on {delta_src_curdir_path!r}, skip"
                    )
                    dirnames.clear()
                    continue

                # ------ check whether we should skip this folder ------ #
                dir_should_fully_scan = self._check_dir_should_fully_scan(
                    canonical_curdir_path
                )

                if self._check_skip_dir(canonical_curdir_path):
                    dirnames.clear()
                    continue

                # skip files that over the max_filenum_per_folder,
                # and add these files to remove list
                if len(filenames) > self.MAX_FILENUM_PER_FOLDER:
                    logger.warning(
                        f"reach max_filenum_per_folder on {delta_src_curdir_path}, "
                        "exceeded files will be ignored silently"
                    )

                # process the files under this dir
                for fname in filenames[: self.MAX_FILENUM_PER_FOLDER]:
                    delta_src_fpath = delta_src_curdir_path / fname
                    logger.debug(f"[process_delta_src] process {delta_src_fpath}")
                    # NOTE: should ALWAYS use canonical_fpath in RegularInf and in rm_list
                    canonical_fpath = canonical_curdir_path / fname

                    # ignore non-file file(include symlink)
                    # NOTE: for in-place update, we will recreate all the symlinks,
                    #       so we first remove all the symlinks
                    # NOTE: is_file also return True on symlink points to regular file!
                    if delta_src_fpath.is_symlink() or not delta_src_fpath.is_file():
                        continue

                    # in default match_only mode, if the fpath doesn't exist in new, ignore
                    if (
                        not dir_should_fully_scan
                        and not self._fstable_orm.orm_select_entry(
                            path=str(canonical_fpath)
                        )
                    ):
                        continue

                    self._max_pending_tasks.acquire()
                    pool.submit(
                        self._process_file,
                        delta_src_fpath,
                        thread_local=thread_local,
                    ).add_done_callback(self._task_done_callback)
        finally:
            pool.shutdown(wait=True)
            self._fstable_orm._con.close()
            self._rstable_orm.orm_pool_shutdown()
