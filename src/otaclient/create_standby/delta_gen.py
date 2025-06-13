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
import time
from abc import abstractmethod
from hashlib import sha256
from pathlib import Path
from queue import Queue
from typing import Generic, TypedDict, TypeVar

from ota_metadata.file_table.db import FileTableDirORM, FileTableRegularORMPool
from ota_metadata.legacy2.metadata import OTAMetadata
from ota_metadata.legacy2.rs_table import ResourceTableORMPool
from otaclient._status_monitor import StatusReport, UpdateProgressReport
from otaclient.configs.cfg import cfg
from otaclient.create_standby.utils import TopDownCommonShortestPath
from otaclient_common import EMPTY_FILE_SHA256, EMPTY_FILE_SHA256_BYTE, replace_root
from otaclient_common._typing import StrOrPath

logger = logging.getLogger(__name__)

T = TypeVar("T")

CANONICAL_ROOT = cfg.CANONICAL_ROOT
CANONICAL_ROOT_P = Path(CANONICAL_ROOT)

DB_CONN_NUMS = 3

PROCESS_FILES_REPORT_INTERVAL = cfg.PROCESS_FILES_REPORT_INTERVAL


class UpdateStandbySlotFailed(Exception): ...


class DeltaGenParams(TypedDict):
    ota_metadata: OTAMetadata
    delta_src: Path
    copy_dst: Path
    status_report_queue: Queue[StatusReport]
    session_id: str


class _DeltaGeneratorBase:
    """
    NOTE: the instance of this class cannot be re-used after delta is generated.
    """

    CLEANUP_ENTRY = {Path("/lost+found"), Path("/tmp"), Path("/run")}
    """Entries we need to cleanup everytime, include /lost+found, /tmp and /run."""

    OTA_WORK_PATHS = {Path(cfg.OTA_TMP_STORE), Path(cfg.OTA_TMP_META_STORE)}
    """Folders for otaclient working on standby slot."""

    def __init__(
        self,
        *,
        ota_metadata: OTAMetadata,
        delta_src: Path,
        copy_dst: Path,
        status_report_queue: Queue[StatusReport],
        session_id: str,
    ) -> None:
        self._status_report_queue = status_report_queue
        self.session_id = session_id
        self._delta_src_mount_point = delta_src
        self._copy_dst = copy_dst
        self._ota_metadata = ota_metadata

        self._ft_reg_orm = FileTableRegularORMPool(
            con_factory=ota_metadata.connect_fstable, number_of_cons=DB_CONN_NUMS
        )
        self._ft_dir_orm = FileTableDirORM(ota_metadata.connect_fstable())
        self._rst_orm_pool = ResourceTableORMPool(
            con_factory=ota_metadata.connect_rstable, number_of_cons=DB_CONN_NUMS
        )

        self._que = Queue()
        self._max_pending_tasks = threading.Semaphore(
            cfg.MAX_CONCURRENT_PROCESS_FILE_TASKS
        )
        self._dirs_to_remove = dtr = TopDownCommonShortestPath()
        for _p in self.CLEANUP_ENTRY:
            dtr.add_path(Path(_p))

        # put the empty file into copy_dst, remember to commit a record for preparing the empty file
        (copy_dst / EMPTY_FILE_SHA256).touch()
        self._rst_orm_pool.orm_delete_entries(digest=EMPTY_FILE_SHA256_BYTE)
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=UpdateProgressReport(
                    operation=UpdateProgressReport.Type.PREPARE_LOCAL_COPY,
                    processed_file_num=1,
                ),
                session_id=self.session_id,
            )
        )


class ProcessFileHelper(Generic[T]):
    def __init__(
        self,
        _que: Queue[T],
        *,
        session_id: str,
        report_que: Queue[StatusReport],
        report_interval: int = cfg.PROCESS_FILES_REPORT_INTERVAL,
    ) -> None:
        self._que = _que
        self._status_report_queue = report_que
        self.session_id = session_id
        self.report_interval = report_interval

        self._hash_buffer = hash_buffer = bytearray(cfg.READ_CHUNK_SIZE)
        self._hash_bufferview = memoryview(hash_buffer)
        self._next_report = 0
        self._merged_payload = UpdateProgressReport(
            operation=UpdateProgressReport.Type.PREPARE_LOCAL_COPY
        )

    def process_one_file(self, fpath: StrOrPath) -> tuple[bytes, int]:
        hash_f, file_size = sha256(), 0
        with open(fpath, "rb") as src:
            while read_size := src.readinto(self._hash_buffer):
                file_size += read_size
                hash_f.update(self._hash_bufferview[:read_size])
        return hash_f.digest(), file_size

    def report_one_file(self, *file_size: int, force_report: bool = False) -> None:
        for _fsize in file_size:
            self._merged_payload.processed_file_size += _fsize
            self._merged_payload.processed_file_num += 1

        _now = time.perf_counter()
        if force_report or _now > self._next_report:
            self._next_report = _now + self.report_interval
            self._status_report_queue.put_nowait(
                StatusReport(payload=self._merged_payload, session_id=self.session_id)
            )
            self._merged_payload = UpdateProgressReport(
                operation=UpdateProgressReport.Type.PREPARE_LOCAL_COPY
            )


#
# ------ delta generation by full disk scan ------ #
#


class DeltaGenFullDiskScan(_DeltaGeneratorBase):
    _que: Queue[tuple[Path, Path, bool] | None]

    # entry under the following folders will be scanned
    # no matter it is existed in new image or not
    FULL_SCAN_PATHS = {
        Path("/lib"),
        Path("/var/lib"),
        Path("/usr"),
        Path("/opt/nvidia"),
        Path("/home/autoware/autoware.proj"),
    }

    # entries start with the following paths will be ignored
    EXCLUDE_PATHS = {
        Path("/tmp"),
        Path("/dev"),
        Path("/proc"),
        Path("/sys"),
        Path("/lost+found"),
        Path("/media"),
        Path("/mnt"),
        Path("/run"),
        Path("/srv"),
    }

    # introduce limitations here to prevent unexpected
    # scanning in unknown large, deep folders in full
    # scan mode.
    # NOTE: the following settings are enough for most cases.
    # NOTE: since v3.9, we change the standby slot mount point to /run/otaclient/mnt/standby_slot,
    #       so extend the maximum folders depth.
    MAX_FOLDER_DEEPTH = 27
    MAX_FILENUM_PER_FOLDER = 8192

    def _check_if_need_to_process_dir(
        self, canonical_curdir_path: Path
    ) -> tuple[bool, bool]:
        """
        Returns: dir_should_be_processed, dir_should_be_fully_scanned
        """
        if canonical_curdir_path == CANONICAL_ROOT_P:
            return True, False

        # ------ check dir search deepth ------ #
        if len(canonical_curdir_path.parents) > self.MAX_FOLDER_DEEPTH:
            logger.warning(
                f"{canonical_curdir_path=} exceeds {self.MAX_FOLDER_DEEPTH=}, skip scan this folder"
            )
            return False, False

        # ------ check dir should be skipped ------ #
        dir_should_fully_scan = False
        for _cur_parent in reversed(canonical_curdir_path.parents):
            if _cur_parent in self.FULL_SCAN_PATHS:
                dir_should_fully_scan = True
                break
            if _cur_parent in self.EXCLUDE_PATHS:
                return False, False

        # If current dir is not:
        #   1. the root folder
        #   2. should fully scan folder
        #   3. folder existed in the new OTA image
        # skip scanning this folder and its subfolders.
        _str_canon_fpath = str(canonical_curdir_path)
        if (
            _str_canon_fpath != CANONICAL_ROOT
            and not dir_should_fully_scan
            and not self._ft_dir_orm.orm_check_entry_exist(path=_str_canon_fpath)
        ):
            return False, dir_should_fully_scan
        return True, dir_should_fully_scan

    @abstractmethod
    def _process_file_thread_worker(self):
        raise NotImplementedError

    @abstractmethod
    def _cleanup_base(self):
        raise NotImplementedError

    @abstractmethod
    def _calculate_delta(self):
        raise NotImplementedError

    def process_slot(self, *, worker_num: int = cfg.MAX_PROCESS_FILE_THREAD) -> None:
        _workers: list[threading.Thread] = []
        try:
            try:
                for _ in range(worker_num):
                    _t = threading.Thread(
                        target=self._process_file_thread_worker, daemon=True
                    )
                    _t.start()
                    _workers.append(_t)
                self._calculate_delta()
            finally:
                self._que.put_nowait(None)
                for _t in _workers:
                    _t.join()

            # heals the hole of the rs table
            self._rst_orm_pool.orm_execute("VACUUM;")
            self._cleanup_base()
        finally:
            self._rst_orm_pool.orm_pool_shutdown()
            self._ft_reg_orm.orm_pool_shutdown()
            self._ft_dir_orm.orm_con.close()


class InPlaceDeltaGenFullDiskScan(DeltaGenFullDiskScan):
    def _process_file_thread_worker(self) -> None:
        """Thread worker to scan files."""
        worker_helper = ProcessFileHelper(
            self._que,
            session_id=self.session_id,
            report_que=self._status_report_queue,
        )

        fpath = None
        while _input := self._que.get():
            try:
                fpath, canonical_fpath, fully_scan = _input

                # for in-place update mode, if fully_scan==False, and the file doesn't present in new,
                #   just directly remove it.
                if not fully_scan and not self._ft_reg_orm.orm_check_entry_exist(
                    path=str(canonical_fpath)
                ):
                    continue

                calculated_digest, file_size = worker_helper.process_one_file(fpath)

                # If the resource we scan here is listed in the resouce table, prepare it
                #   to the copy_dir at standby slot for later use.
                if self._rst_orm_pool.orm_delete_entries(digest=calculated_digest) == 1:
                    resource_save_dst = self._copy_dst / calculated_digest.hex()
                    os.link(fpath, resource_save_dst, follow_symlinks=False)
                    worker_helper.report_one_file(file_size)
            except BaseException:
                continue
            finally:
                # remove the original file as we will recreate file entries later
                if fpath:
                    fpath.unlink(missing_ok=True)
                self._max_pending_tasks.release()  # always release se first

        # commit the final batch
        worker_helper.report_one_file(force_report=True)
        # wake up other threads
        self._que.put_nowait(None)

    def _calculate_delta(self) -> None:
        logger.debug("process delta src and generate delta...")

        for curdir, dirnames, filenames in os.walk(
            self._delta_src_mount_point, topdown=True, followlinks=False
        ):
            delta_src_curdir_path = Path(curdir)
            canonical_curdir_path = Path(
                replace_root(
                    delta_src_curdir_path,
                    self._delta_src_mount_point,
                    CANONICAL_ROOT_P,
                )
            )
            if canonical_curdir_path in self.OTA_WORK_PATHS:
                continue  # skip scanning OTA work paths

            _dir_should_process, _dir_should_fully_scan = (
                self._check_if_need_to_process_dir(canonical_curdir_path)
            )
            if not _dir_should_process:
                dirnames.clear()
                self._dirs_to_remove.add_path(canonical_curdir_path)
                continue

            # remove any symlinks of directory under current dir
            for _dname in dirnames:
                _dir = delta_src_curdir_path / _dname
                if _dir.is_symlink():
                    _dir.unlink(missing_ok=True)

            # skip files that over the max_filenum_per_folder,
            # and add these files to remove list
            if len(filenames) > self.MAX_FILENUM_PER_FOLDER:
                logger.warning(
                    f"reach max_filenum_per_folder on {delta_src_curdir_path}, "
                    "exceeded files will be cleaned up unconditionally"
                )
                for _fname in filenames[self.MAX_FILENUM_PER_FOLDER :]:
                    (delta_src_curdir_path / _fname).unlink(missing_ok=True)

            for fname in filenames[: self.MAX_FILENUM_PER_FOLDER]:
                delta_src_fpath = delta_src_curdir_path / fname

                # cleanup non-file file(include symlink)
                # NOTE: we will recreate all the symlinks,
                #       so we first remove all the symlinks
                # NOTE: is_file also return True on symlink points to regular file!
                if delta_src_fpath.is_symlink() or not delta_src_fpath.is_file():
                    delta_src_fpath.unlink(missing_ok=True)
                    continue

                if not delta_src_fpath.is_file() or delta_src_fpath.stat().st_size == 0:
                    delta_src_fpath.unlink(missing_ok=True)
                    continue  # skip empty file

                self._max_pending_tasks.acquire()
                self._que.put_nowait(
                    (
                        delta_src_fpath,
                        canonical_curdir_path / fname,
                        _dir_should_fully_scan,
                    )
                )

    def _cleanup_base(self):
        # NOTE: the dirs in dirs_to_remove is cannonical dirs!
        for _canon_dir in self._dirs_to_remove.iter_paths():
            if _canon_dir in self.OTA_WORK_PATHS:
                continue  # NOTE: DO NOT cleanup OTA work paths!

            _delta_src_dir = replace_root(
                _canon_dir, CANONICAL_ROOT, self._delta_src_mount_point
            )
            shutil.rmtree(_delta_src_dir, ignore_errors=True)


class RebuildDeltaGenFullDiskScan(DeltaGenFullDiskScan):
    """
    In rebuild mode, base will be the active slot, which we should not modify.
    """

    def _process_file_thread_worker(self) -> None:
        """Thread worker to scan files."""
        worker_helper = ProcessFileHelper(
            self._que,
            session_id=self.session_id,
            report_que=self._status_report_queue,
        )

        while _input := self._que.get():
            try:
                fpath, canonical_fpath, fully_scan = _input

                # for rebuild update mode, if fully_scan==False, and the file doesn't present in new,
                #   just directly skip it.
                if not fully_scan and not self._ft_reg_orm.orm_check_entry_exist(
                    path=str(canonical_fpath)
                ):
                    continue

                calculated_digest, file_size = worker_helper.process_one_file(fpath)

                # If the resource we scan here is listed in the resouce table, COPY it
                #   to the copy_dir at standby slot for later use.
                if self._rst_orm_pool.orm_delete_entries(digest=calculated_digest) == 1:
                    resource_save_dst = self._copy_dst / calculated_digest.hex()
                    shutil.copy(fpath, resource_save_dst, follow_symlinks=False)
                    worker_helper.report_one_file(file_size)
            except BaseException:
                continue
            finally:
                self._max_pending_tasks.release()  # always release se first

        # commit the final batch
        worker_helper.report_one_file(force_report=True)
        # wake up other threads
        self._que.put_nowait(None)

    def _calculate_delta(self) -> None:
        logger.debug("process delta src and generate delta...")

        for curdir, dirnames, filenames in os.walk(
            self._delta_src_mount_point, topdown=True, followlinks=False
        ):
            delta_src_curdir_path = Path(curdir)
            canonical_curdir_path = Path(
                replace_root(
                    delta_src_curdir_path,
                    self._delta_src_mount_point,
                    CANONICAL_ROOT_P,
                )
            )
            if canonical_curdir_path in self.OTA_WORK_PATHS:
                continue  # skip scanning OTA work paths

            _dir_should_process, _dir_should_fully_scan = (
                self._check_if_need_to_process_dir(canonical_curdir_path)
            )
            if not _dir_should_process:
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
                # NOTE: is_file also return True on symlink points to regular file!
                if delta_src_fpath.is_symlink() or not delta_src_fpath.is_file():
                    continue

                if not delta_src_fpath.is_file() or delta_src_fpath.stat().st_size == 0:
                    continue  # skip empty file

                self._max_pending_tasks.acquire()
                self._que.put_nowait(
                    (
                        delta_src_fpath,
                        canonical_curdir_path / fname,
                        _dir_should_fully_scan,
                    )
                )

    def _cleanup_base(self) -> None:
        # NOTE: for rebuild mode, we will not cleanup the base.
        return


#
# ------ delta generation with base filetable assisted ------ #
#


class DeltaWithBaseFileTable(_DeltaGeneratorBase):
    _que: Queue[tuple[bytes, list[Path]] | None]

    @abstractmethod
    def _process_file_thread_worker(self):
        raise NotImplementedError

    def _calculate_delta(self, base_fst: str):
        logger.debug("process delta src and generate delta...")

        for _input in self._ota_metadata.iter_common_regular_entries_by_digest(
            base_fst
        ):
            self._max_pending_tasks.acquire()
            self._que.put_nowait(_input)

    @abstractmethod
    def _cleanup_base(self):
        raise NotImplementedError

    def process_slot(
        self, base_file_table_db: str, *, worker_num: int = cfg.MAX_PROCESS_FILE_THREAD
    ) -> None:
        _workers: list[threading.Thread] = []
        try:
            try:
                for _ in range(worker_num):
                    _t = threading.Thread(
                        target=self._process_file_thread_worker, daemon=True
                    )
                    _t.start()
                    _workers.append(_t)
                self._calculate_delta(base_file_table_db)
            finally:
                self._que.put_nowait(None)
                for _t in _workers:
                    _t.join()

            # heals the hole of the rs table
            self._rst_orm_pool.orm_execute("VACUUM;")
            self._cleanup_base()
        finally:
            self._rst_orm_pool.orm_pool_shutdown()
            self._ft_reg_orm.orm_pool_shutdown()
            self._ft_dir_orm.orm_con.close()


class InPlaceDeltaWithBaseFileTable(DeltaWithBaseFileTable):
    def _process_file_thread_worker(self) -> None:
        """Thread worker to scan files."""
        worker_helper = ProcessFileHelper(
            self._que,
            session_id=self.session_id,
            report_que=self._status_report_queue,
        )

        delta_src_fpaths = None
        while _input := self._que.get():
            try:
                expected_digest, fpaths = _input
                delta_src_fpaths = [
                    Path(
                        replace_root(
                            _fpath,
                            CANONICAL_ROOT,
                            self._delta_src_mount_point,
                        )
                    )
                    for _fpath in fpaths
                ]

                for fpath in delta_src_fpaths:
                    try:
                        calculated_digest, file_size = worker_helper.process_one_file(
                            fpath
                        )
                    except BaseException:
                        continue

                    if calculated_digest == expected_digest:
                        if (
                            self._rst_orm_pool.orm_delete_entries(
                                digest=expected_digest
                            )
                            == 1
                        ):
                            resource_save_dst = self._copy_dst / expected_digest.hex()
                            os.link(fpath, resource_save_dst, follow_symlinks=False)
                            worker_helper.report_one_file(file_size)
                        # directly break out once we found a valid resource, no matter
                        #   we finally need it or not.
                        break
            except BaseException:
                continue
            finally:
                # after the resource is collected, remove the original file
                if delta_src_fpaths:
                    for _f in delta_src_fpaths:
                        _f.unlink(missing_ok=True)
                self._max_pending_tasks.release()  # always release se first

        # commit the final batch
        worker_helper.report_one_file(force_report=True)
        # wake up other threads
        self._que.put_nowait(None)

    def _cleanup_base(self):
        _canonical_root = Path(CANONICAL_ROOT)
        for curdir, dirnames, filenames in os.walk(
            self._delta_src_mount_point, topdown=True, followlinks=False
        ):
            delta_src_curdir_path = Path(curdir)
            canonical_curdir_path = Path(
                replace_root(
                    delta_src_curdir_path,
                    self._delta_src_mount_point,
                    _canonical_root,
                )
            )
            # NOTE: DO NOT CLEANUP the OTA resource folder!
            if canonical_curdir_path in self.OTA_WORK_PATHS:
                continue

            if not (
                canonical_curdir_path == _canonical_root
                or self._ft_dir_orm.orm_check_entry_exist(
                    path=str(canonical_curdir_path)
                )
            ):
                dirnames.clear()
                shutil.rmtree(delta_src_curdir_path, ignore_errors=True)
                continue

            # NOTE: remove the dir symlinks
            for _dname in dirnames:
                _dpath = delta_src_curdir_path / _dname
                if _dpath.is_symlink():
                    _dpath.unlink(missing_ok=True)

            for _fname in filenames:
                _fpath = delta_src_curdir_path / _fname
                _fpath.unlink(missing_ok=True)


class RebuildDeltaWithBaseFileTable(DeltaWithBaseFileTable):
    """
    In rebuild mode, base will be the active slot, which we should not modify.
    """

    def _process_file_thread_worker(self) -> None:
        """Thread worker to scan files."""
        worker_helper = ProcessFileHelper(
            self._que,
            session_id=self.session_id,
            report_que=self._status_report_queue,
        )

        while _input := self._que.get():
            try:
                expected_digest, fpaths = _input

                for fpath in fpaths:
                    try:
                        calculated_digest, file_size = worker_helper.process_one_file(
                            fpath
                        )
                    except BaseException:
                        continue

                    if calculated_digest == expected_digest:
                        if (
                            self._rst_orm_pool.orm_delete_entries(
                                digest=expected_digest
                            )
                            == 1
                        ):
                            resource_save_dst = self._copy_dst / expected_digest.hex()
                            shutil.copy(fpath, resource_save_dst, follow_symlinks=False)
                            worker_helper.report_one_file(file_size)
                        # directly break out once we found a valid resource, no matter
                        #   we finally need it or not.
                        break
            except BaseException:
                continue
            finally:
                self._max_pending_tasks.release()  # always release se first

        # commit the final batch
        worker_helper.report_one_file(force_report=True)
        # wake up other threads
        self._que.put_nowait(None)

    def _cleanup_base(self):
        # NOTE: in rebuild mode, we do not cleanup base
        return
