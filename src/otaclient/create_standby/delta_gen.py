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

import contextlib
import logging
import os
import shutil
import threading
import time
from abc import abstractmethod
from hashlib import sha256
from itertools import chain
from pathlib import Path
from queue import Queue
from typing import Generic, Iterator, NamedTuple, TypedDict, TypeVar

from ota_image_libs.v1.file_table.db import FileTableDBHelper

from otaclient._status_monitor import StatusReport, UpdateProgressReport
from otaclient.configs.cfg import cfg
from otaclient.create_standby.utils import TopDownCommonShortestPath
from otaclient_common import replace_root
from otaclient_common._io import _gen_tmp_fname, remove_file
from otaclient_common._typing import StrOrPath
from otaclient_common.linux import is_directory, is_non_empty_regular_file

from ._common import ResourcesDigestWithSize

logger = logging.getLogger(__name__)

T = TypeVar("T")

CANONICAL_ROOT = cfg.CANONICAL_ROOT
CANONICAL_ROOT_P = Path(CANONICAL_ROOT)

DB_CONN_NUMS = 3

PROCESS_FILES_REPORT_INTERVAL = cfg.PROCESS_FILES_REPORT_INTERVAL

# introduce limitations here to prevent unexpected
# scanning in unknown large, deep folders in full
# scan mode.
# NOTE: the following settings are enough for most cases.
# NOTE: since v3.9, we change the standby slot mount point to /run/otaclient/mnt/standby_slot,
#       so extend the maximum folders depth.
MAX_FOLDER_DEEPTH = 35
MAX_FILENUM_PER_FOLDER = 8192


class UpdateStandbySlotFailed(Exception): ...


class DeltaGenParams(TypedDict):
    file_table_db_helper: FileTableDBHelper
    all_resource_digests: ResourcesDigestWithSize
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

    OTA_WORKING_DIRS = {
        cfg.OTA_RESOURCES_STORE,
        cfg.OTA_META_STORE,
    }
    """Folders for otaclient working on standby slot."""

    @classmethod
    def is_ota_working_dir(cls, _path: str) -> bool:
        return any(_path.startswith(_ota_wd) for _ota_wd in cls.OTA_WORKING_DIRS)

    def __init__(
        self,
        *,
        file_table_db_helper: FileTableDBHelper,
        all_resource_digests: ResourcesDigestWithSize,
        delta_src: Path,
        copy_dst: Path,
        status_report_queue: Queue[StatusReport],
        session_id: str,
    ) -> None:
        self._fst_db_helper = file_table_db_helper
        self._status_report_queue = status_report_queue
        self.session_id = session_id
        self._delta_src_mount_point = delta_src
        self._copy_dst = copy_dst

        self._ft_reg_orm_pool = file_table_db_helper.get_regular_file_orm_pool(
            db_conn_num=DB_CONN_NUMS
        )
        self._ft_dir_orm = file_table_db_helper.get_dir_orm()
        self._resources_to_check = all_resource_digests
        """Resources that we need to collecting during delta_calculation."""

        self._que = Queue()
        self._max_pending_tasks = threading.Semaphore(
            cfg.MAX_CONCURRENT_PROCESS_FILE_TASKS
        )
        self._dirs_to_remove = dtr = TopDownCommonShortestPath()
        for _p in self.CLEANUP_ENTRY:
            dtr.add_path(Path(_p))


def _cleanup_all_files_under_folder(_dir: Path, _names: Iterator[str]) -> None:
    """Cleanup all non-directory files under a folder."""
    for _name in _names:
        _f = _dir / _name
        if not is_directory(_f):
            _f.unlink(missing_ok=True)


class ProcessFileHelper(Generic[T]):
    def __init__(
        self,
        _que: Queue[T],
        *,
        session_id: str,
        report_que: Queue[StatusReport],
        report_interval: int = cfg.PROCESS_FILES_REPORT_INTERVAL,
        buffer_size: int = cfg.READ_CHUNK_SIZE,
    ) -> None:
        self._que = _que
        self._status_report_queue = report_que
        self.session_id = session_id
        self.report_interval = report_interval

        self._hash_buffer = hash_buffer = bytearray(buffer_size)
        self._hash_bufferview = memoryview(hash_buffer)
        self._next_report = 0
        self._merged_payload = UpdateProgressReport(
            operation=UpdateProgressReport.Type.PREPARE_LOCAL_COPY
        )

    def verify_file(self, fpath: StrOrPath) -> tuple[bytes, int]:
        hash_f, file_size = sha256(), 0
        with open(fpath, "rb") as src:
            src_fd = src.fileno()
            # NOTE(20250616): hint kernel that we will read the whole file,
            #                 kernel will enable read-ahead cache and discard
            #                 file cache behind.
            os.posix_fadvise(src_fd, 0, 0, os.POSIX_FADV_NOREUSE)
            os.posix_fadvise(src_fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)
            while read_size := src.readinto(self._hash_buffer):
                file_size += read_size
                hash_f.update(self._hash_bufferview[:read_size])
            # NOTE(20250616): hint kernel to drop the file cache pages.
            os.posix_fadvise(src_fd, 0, 0, os.POSIX_FADV_DONTNEED)
        return hash_f.digest(), file_size

    def stream_and_verify_file(
        self, fpath: StrOrPath, dst_fpath: StrOrPath
    ) -> tuple[bytes, int]:
        hash_f, file_size = sha256(), 0
        with open(fpath, "rb") as src, open(dst_fpath, "wb") as dst:
            src_fd, dst_fd = src.fileno(), dst.fileno()
            # NOTE(20250616): hint kernel that we will read the whole file,
            #                 kernel will enable read-ahead cache and discard
            #                 file cache behind.
            os.posix_fadvise(src_fd, 0, 0, os.POSIX_FADV_NOREUSE)
            os.posix_fadvise(dst_fd, 0, 0, os.POSIX_FADV_NOREUSE)
            os.posix_fadvise(src_fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)
            while read_size := src.readinto(self._hash_buffer):
                file_size += read_size
                hash_f.update(self._hash_bufferview[:read_size])
                # zero-copy write
                dst.write(self._hash_bufferview[:read_size])

            # NOTE(20250616): hint kernel to drop the file cache pages.
            os.posix_fadvise(src_fd, 0, 0, os.POSIX_FADV_DONTNEED)
            os.posix_fadvise(dst_fd, 0, 0, os.POSIX_FADV_DONTNEED)
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


class _CheckDirResult(NamedTuple):
    dir_should_be_removed: bool
    """Whether this folder presents in the new image."""

    dir_should_be_fully_scan: bool
    """Whether delta generator should scan all files under this folder,
    including files don't present in the new image.
    """

    @property
    def dir_should_be_process(self) -> bool:
        """Whether delta generator should look into this folder."""
        return self.dir_should_be_fully_scan or not self.dir_should_be_removed


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

    def _check_if_need_to_process_dir(
        self, canonical_curdir_path: Path
    ) -> _CheckDirResult:
        if canonical_curdir_path == CANONICAL_ROOT_P:
            return _CheckDirResult(
                dir_should_be_removed=False,
                dir_should_be_fully_scan=False,
            )

        # ------ check dir search deepth ------ #
        if len(canonical_curdir_path.parents) > MAX_FOLDER_DEEPTH:
            logger.warning(
                f"{canonical_curdir_path=} exceeds {MAX_FOLDER_DEEPTH=}, skip scan this folder"
            )
            return _CheckDirResult(
                dir_should_be_removed=True,
                dir_should_be_fully_scan=False,
            )

        # ------ check dir should be skipped and/or removed ------ #
        dir_should_be_fully_scan = False
        for _cur_parent in reversed(canonical_curdir_path.parents):
            if _cur_parent in self.FULL_SCAN_PATHS:
                dir_should_be_fully_scan = True
                break
            if _cur_parent in self.EXCLUDE_PATHS:
                return _CheckDirResult(
                    dir_should_be_removed=True,
                    dir_should_be_fully_scan=False,
                )

        _str_canon_fpath = str(canonical_curdir_path)
        dir_should_be_removed = not self._ft_dir_orm.orm_check_entry_exist(
            path=_str_canon_fpath
        )
        return _CheckDirResult(
            dir_should_be_removed=dir_should_be_removed,
            dir_should_be_fully_scan=dir_should_be_fully_scan,
        )

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
            self._cleanup_base()
        finally:
            self._ft_reg_orm_pool.orm_pool_shutdown()
            self._ft_dir_orm.orm_con.close()


class InPlaceDeltaGenFullDiskScan(DeltaGenFullDiskScan):
    """Calculate delta with inplace mode + full disk scan.

    We will generate delta from the standby slot and them clean up
        unused files on the standby slot inplace.
    """

    def _process_file_thread_worker(self) -> None:
        """Thread worker to scan files."""
        worker_helper = ProcessFileHelper(
            self._que,
            session_id=self.session_id,
            report_que=self._status_report_queue,
        )

        while _input := self._que.get():
            fpath, canonical_fpath, fully_scan = _input
            try:
                # for in-place update mode, if fully_scan==False, and the file doesn't present in new,
                #   just directly remove it.
                if not fully_scan and not self._ft_reg_orm_pool.orm_check_entry_exist(
                    path=str(canonical_fpath)
                ):
                    continue

                calculated_digest, file_size = worker_helper.verify_file(fpath)

                # If the resource we scan here is listed in the resouce table, prepare it
                #   to the copy_dir at standby slot for later use.
                with contextlib.suppress(KeyError):
                    self._resources_to_check.pop(calculated_digest)

                    resource_save_dst = self._copy_dst / calculated_digest.hex()
                    os.replace(fpath, resource_save_dst)
                    worker_helper.report_one_file(file_size)
            except BaseException:
                continue
            finally:
                self._max_pending_tasks.release()  # always release se first
                # remove the original file as we will recreate file entries later
                fpath.unlink(missing_ok=True)

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
            canonical_curdir = replace_root(
                delta_src_curdir_path,
                self._delta_src_mount_point,
                CANONICAL_ROOT_P,
            )
            canonical_curdir_path = Path(canonical_curdir)

            if self.is_ota_working_dir(canonical_curdir):
                continue  # skip scanning OTA work paths

            _check_dir = self._check_if_need_to_process_dir(canonical_curdir_path)
            # NOTE: when dir doesn't exist in the new image, if it is a full_scan dir
            #       we might still need to look into it.
            if _check_dir.dir_should_be_removed:
                self._dirs_to_remove.add_path(canonical_curdir_path)

            if not _check_dir.dir_should_be_process:
                dirnames.clear()
                continue

            # remove any symlinks of directory under current dir
            for _dname in dirnames:
                _dir = delta_src_curdir_path / _dname
                if _dir.is_symlink():
                    _dir.unlink(missing_ok=True)

            # skip files that over the max_filenum_per_folder,
            # and add these files to remove list
            if len(filenames) > MAX_FILENUM_PER_FOLDER:
                logger.warning(
                    f"reach max_filenum_per_folder on {delta_src_curdir_path}, "
                    "exceeded files will be cleaned up unconditionally"
                )
                for _fname in filenames[MAX_FILENUM_PER_FOLDER:]:
                    (delta_src_curdir_path / _fname).unlink(missing_ok=True)

            for fname in filenames[:MAX_FILENUM_PER_FOLDER]:
                delta_src_fpath = delta_src_curdir_path / fname

                # cleanup non-file file(include symlink) and empty file
                # NOTE: we will recreate all the symlinks,
                #       so we first remove all the symlinks
                # NOTE: is_file also return True on symlink points to regular file!
                if not is_non_empty_regular_file(delta_src_fpath):
                    delta_src_fpath.unlink(missing_ok=True)
                else:
                    self._max_pending_tasks.acquire()
                    self._que.put_nowait(
                        (
                            delta_src_fpath,
                            canonical_curdir_path / fname,
                            _check_dir.dir_should_be_fully_scan,
                        )
                    )

    def _cleanup_base(self):
        # NOTE: the dirs in dirs_to_remove is cannonical dirs!
        for _canon_dir in self._dirs_to_remove.iter_paths():
            if self.is_ota_working_dir(str(_canon_dir)):
                continue  # NOTE: DO NOT cleanup OTA work paths!

            _delta_src_dir = replace_root(
                _canon_dir, CANONICAL_ROOT, self._delta_src_mount_point
            )
            shutil.rmtree(_delta_src_dir, ignore_errors=True)


class RebuildDeltaGenFullDiskScan(DeltaGenFullDiskScan):
    """Calculate delta with rebuild mode + full disk scan.

    We will completely cleanup standby slot, generating delta from scanning active slot and
        sending the delta to standby slot for rebuilding the whole standby slot.
    """

    def _process_file_thread_worker(self) -> None:
        """Thread worker to scan files."""
        worker_helper = ProcessFileHelper(
            self._que,
            session_id=self.session_id,
            report_que=self._status_report_queue,
        )

        while _input := self._que.get():
            tmp_dst_fpath = self._copy_dst / _gen_tmp_fname()
            fpath, canonical_fpath, fully_scan = _input
            try:
                # for rebuild update mode, if fully_scan==False, and the file doesn't present in new,
                #   just directly skip it.
                if not fully_scan and not self._ft_reg_orm_pool.orm_check_entry_exist(
                    path=str(canonical_fpath)
                ):
                    continue

                # If the resource we scan here is listed in the resouce table, COPY it
                #   to the copy_dir at standby slot for later use.
                calculated_digest, file_size = worker_helper.stream_and_verify_file(
                    fpath, tmp_dst_fpath
                )
                resource_save_dst = self._copy_dst / calculated_digest.hex()

                with contextlib.suppress(KeyError):
                    self._resources_to_check.pop(calculated_digest)

                    if not resource_save_dst.is_file():
                        os.replace(tmp_dst_fpath, resource_save_dst)
                        worker_helper.report_one_file(file_size)
            except BaseException:
                continue
            finally:
                self._max_pending_tasks.release()  # always release se first
                tmp_dst_fpath.unlink(missing_ok=True)

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
            canonical_curdir = replace_root(
                delta_src_curdir_path,
                self._delta_src_mount_point,
                CANONICAL_ROOT_P,
            )
            canonical_curdir_path = Path(canonical_curdir)

            if self.is_ota_working_dir(canonical_curdir):
                continue  # skip scanning OTA work paths

            _check_dir = self._check_if_need_to_process_dir(canonical_curdir_path)
            if not _check_dir.dir_should_be_process:
                dirnames.clear()
                continue

            # skip files that over the max_filenum_per_folder,
            # and add these files to remove list
            if len(filenames) > MAX_FILENUM_PER_FOLDER:
                logger.warning(
                    f"reach max_filenum_per_folder on {delta_src_curdir_path}, "
                    "exceeded files will be ignored silently"
                )

            for fname in filenames[:MAX_FILENUM_PER_FOLDER]:
                delta_src_fpath = delta_src_curdir_path / fname

                # ignore non-file file(include symlink)
                # NOTE: is_file also return True on symlink points to regular file!
                if not is_non_empty_regular_file(delta_src_fpath):
                    continue

                self._max_pending_tasks.acquire()
                self._que.put_nowait(
                    (
                        delta_src_fpath,
                        canonical_curdir_path / fname,
                        _check_dir.dir_should_be_fully_scan,
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

        for _input in self._fst_db_helper.iter_common_regular_entries_by_digest(
            base_fst
        ):
            _digest, _ = _input
            # NOTE(20250820): some of the resources might be already prepared
            #                 by the resume OTA, skip collecting these resources.
            if _digest not in self._resources_to_check:
                continue

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
            logger.info("start thread workers to calculate delta ...")
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

            logger.info(
                "clean up files and directories that doesn't present in new image ..."
            )
            self._cleanup_base()
        finally:
            self._ft_reg_orm_pool.orm_pool_shutdown()
            self._ft_dir_orm.orm_con.close()


class InPlaceDeltaWithBaseFileTable(DeltaWithBaseFileTable):
    """Calculate delta with inplace mode + standby slot file_table.

    We will generate delta from the standby slot and them clean up
        the unused files on the standby slot inplace.
    """

    def _process_file_thread_worker(self) -> None:
        """Thread worker to scan files."""
        worker_helper = ProcessFileHelper(
            self._que,
            session_id=self.session_id,
            report_que=self._status_report_queue,
        )

        while _input := self._que.get():
            expected_digest, canonical_fpaths = _input
            delta_src_fpaths = [
                Path(
                    replace_root(
                        _fpath,
                        CANONICAL_ROOT,
                        self._delta_src_mount_point,
                    )
                )
                for _fpath in canonical_fpaths
            ]

            try:
                for fpath in delta_src_fpaths:
                    # NOTE(20250703): a file recorded as regular file on base slot
                    #                 might not be actually a regular file.
                    if not is_non_empty_regular_file(fpath):
                        continue

                    try:
                        calculated_digest, file_size = worker_helper.verify_file(fpath)
                    except BaseException:
                        continue

                    if calculated_digest == expected_digest:
                        with contextlib.suppress(KeyError):
                            self._resources_to_check.pop(expected_digest)

                            resource_save_dst = self._copy_dst / expected_digest.hex()
                            os.replace(fpath, resource_save_dst)
                            worker_helper.report_one_file(file_size)
                        # directly break out once we found a valid resource, no matter
                        #   we finally need it or not.
                        break
            except BaseException:
                continue
            finally:
                self._max_pending_tasks.release()  # always release se first
                # after the resource is collected, remove the original file
                # NOTE(20250703): a file recorded as regular file on base slot
                #                 might not be actually a regular file.
                for _f in delta_src_fpaths:
                    remove_file(_f)

        # commit the final batch
        worker_helper.report_one_file(force_report=True)
        # wake up other threads
        self._que.put_nowait(None)

    def _cleanup_base(self):
        for curdir, dirnames, filenames in os.walk(
            self._delta_src_mount_point, topdown=True, followlinks=False
        ):
            delta_src_curdir_path = Path(curdir)
            canonical_curdir = replace_root(
                delta_src_curdir_path,
                self._delta_src_mount_point,
                CANONICAL_ROOT,
            )
            canonical_curdir_path = Path(canonical_curdir)

            # NOTE: DO NOT CLEANUP the OTA resource folder!
            if self.is_ota_working_dir(canonical_curdir):
                continue

            if canonical_curdir_path == CANONICAL_ROOT_P:
                _cleanup_all_files_under_folder(
                    delta_src_curdir_path, chain(dirnames, filenames)
                )
                continue

            # uncondtionally cleanup folders that exceeds maximum folder deepths.
            if len(canonical_curdir_path.parents) > MAX_FOLDER_DEEPTH:
                dirnames.clear()
                shutil.rmtree(delta_src_curdir_path, ignore_errors=True)
                continue

            dir_should_be_kept = self._ft_dir_orm.orm_check_entry_exist(
                path=str(canonical_curdir_path)
            )
            if dir_should_be_kept:  # preserve the dir, clean up the current folder
                _cleanup_all_files_under_folder(
                    delta_src_curdir_path, chain(dirnames, filenames)
                )
            else:  # this dir doesn't present in new image, fully remove
                dirnames.clear()
                shutil.rmtree(delta_src_curdir_path, ignore_errors=True)


class RebuildDeltaWithBaseFileTable(DeltaWithBaseFileTable):
    """Calculate delta with rebuild mode + active slot file_table.

    We will completely cleanup standby slot, generating delta from scanning active slot with
        assist of active slot's file_table and sending the delta to standby slot for rebuilding
        the whole standby slot.
    """

    def _process_file_thread_worker(self) -> None:
        """Thread worker to scan files."""
        worker_helper = ProcessFileHelper(
            self._que,
            session_id=self.session_id,
            report_que=self._status_report_queue,
        )

        while _input := self._que.get():
            expected_digest, canonical_fpaths = _input
            delta_src_fpaths = {
                Path(
                    replace_root(
                        _fpath,
                        CANONICAL_ROOT,
                        self._delta_src_mount_point,
                    )
                )
                for _fpath in canonical_fpaths
            }

            _tmp_fpath = self._copy_dst / _gen_tmp_fname()
            try:
                for fpath in delta_src_fpaths:
                    try:
                        calculated_digest, file_size = (
                            worker_helper.stream_and_verify_file(fpath, _tmp_fpath)
                        )
                    except BaseException:
                        _tmp_fpath.unlink(missing_ok=True)
                        continue

                    if calculated_digest == expected_digest:
                        with contextlib.suppress(KeyError):
                            self._resources_to_check.pop(expected_digest)

                            resource_save_dst = self._copy_dst / expected_digest.hex()
                            os.replace(_tmp_fpath, resource_save_dst)
                            worker_helper.report_one_file(file_size)
                        # directly break out once we found a valid resource, no matter
                        #   we finally need it or not.
                        break
            except BaseException:
                continue
            finally:
                self._max_pending_tasks.release()  # always release se first
                _tmp_fpath.unlink(missing_ok=True)

        # commit the final batch
        worker_helper.report_one_file(force_report=True)
        # wake up other threads
        self._que.put_nowait(None)

    def _cleanup_base(self):
        # NOTE: in rebuild mode, we do not cleanup base
        return
