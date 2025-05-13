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
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from hashlib import sha256
from pathlib import Path
from queue import Queue
from typing import Generator

from ota_metadata.file_table.db import (
    FileTableDirORM,
    FileTableRegularORMPool,
    RegularFileTypedDict,
    prepare_dir,
    prepare_non_regular,
    prepare_regular,
)
from ota_metadata.legacy2.metadata import OTAMetadata
from ota_metadata.legacy2.rs_table import ResourceTableORMPool
from otaclient._status_monitor import StatusReport, UpdateProgressReport
from otaclient.configs.cfg import cfg
from otaclient_common import EMPTY_FILE_SHA256, replace_root
from otaclient_common._logging import BurstSuppressFilter
from otaclient_common.common import create_tmp_fname
from otaclient_common.retry_task_map import ThreadPoolExecutorWithRetry

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

PROCESS_FILES_REPORT_BATCH = 256
PROCESS_FILES_REPORT_INTERVAL = 1  # second

PROCESS_FILES_CONCURRENCY = 64
PROCESS_FILES_WORKER = cfg.MAX_PROCESS_FILE_THREAD  # 6


class TopDownCommonShortestPath:
    """Assume that the disk scan is top-down style."""

    def __init__(self) -> None:
        self._store: set[Path] = set()

    def add_path(self, _path: Path):
        _path = Path(_path).resolve()
        for _parent in _path.parents:
            # this path is covered by a shorter common prefix
            if _parent in self._store:
                return
        self._store.add(_path)

    def iter_paths(self) -> Generator[Path]:
        yield from self._store


class DeltaGenFullDiskScan:
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

    CLEANUP_ENTRY = {"/lost+found", "/tmp", "/run"}

    # introduce limitations here to prevent unexpected
    # scanning in unknown large, deep folders in full
    # scan mode.
    # NOTE: the following settings are enough for most cases
    MAX_FOLDER_DEEPTH = 23
    MAX_FILENUM_PER_FOLDER = 8192

    def _dir_should_full_scan_or_excluded(
        self, canonical_curdir_path: Path
    ) -> tuple[bool, bool]:
        """
        Returns:
            <should_be_fully_scan, should_be_excluded>
        """
        for parent in reversed(canonical_curdir_path.parents):
            _cur_parent = str(parent)
            if _cur_parent in self.FULL_SCAN_PATHS:
                return True, False
            if _cur_parent in self.EXCLUDE_PATHS:
                return False, True
        return False, False

    def _check_if_need_to_process_dir(
        self, canonical_curdir_path: Path
    ) -> tuple[bool, bool]:
        # ------ check dir search deepth ------ #
        if len(canonical_curdir_path.parents) > self.MAX_FOLDER_DEEPTH:
            logger.warning(
                f"{canonical_curdir_path=} exceeds {self.MAX_FOLDER_DEEPTH=}, skip scan this folder"
            )
            return False, False

        dir_should_fully_scan, dir_is_excluded = self._dir_should_full_scan_or_excluded(
            canonical_curdir_path
        )
        # ------ check dir should be skipped ------ #
        if dir_is_excluded:
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

    def _process_dir(
        self,
        filenames: list[str],
        dir_should_fully_scan: bool,
        *,
        delta_src_curdir_path: Path,
        canonical_curdir_path: Path,
        pool: ThreadPoolExecutor,
        thread_local,
    ) -> None:
        for fname in filenames:
            delta_src_fpath = delta_src_curdir_path / fname

            # ignore non-file file(include symlink)
            # NOTE: for in-place update, we will recreate all the symlinks,
            #       so we first remove all the symlinks
            # NOTE: is_file also return True on symlink points to regular file!
            if delta_src_fpath.is_symlink() or not delta_src_fpath.is_file():
                delta_src_fpath.unlink(missing_ok=True)
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

        self._ft_reg_orm = ft_regular_orm
        self._ft_dir_orm = ft_dir_orm
        self._rst_orm = rs_orm

        self._max_pending_tasks = threading.Semaphore(
            cfg.MAX_CONCURRENT_PROCESS_FILE_TASKS
        )
        self._dirs_to_remove = dtr = TopDownCommonShortestPath()
        for _p in self.EXCLUDE_PATHS:
            dtr.add_path(Path(_p))

        # put the empty file into copy_dst
        (copy_dst / EMPTY_FILE_SHA256).touch()

    @staticmethod
    def _thread_worker_initializer(thread_local) -> None:
        thread_local.buffer = buffer = bytearray(cfg.CHUNK_SIZE)
        thread_local.view = memoryview(buffer)

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

            # for in-place update mode, if fully_scan==False, and the file doesn't present in new,
            #   just directly remove it.
            if not fully_scan and not self._ft_reg_orm.orm_check_entry_exist(
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
                if self._rst_orm.orm_delete_entries(digest=hash_f.digest()) == 1:
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
            burst_suppressed_logger.debug(f"failed to process {fpath}: {e!r}")
        finally:
            # after the resource is collected, remove the original file
            fpath.unlink(missing_ok=True)
            self._max_pending_tasks.release()  # always release se first

    def _calculate_delta(self) -> None:
        logger.debug("process delta src, generate delta and prepare local copy...")
        _canonical_root = Path(CANONICAL_ROOT)

        thread_local = threading.local()

        with ThreadPoolExecutor(
            max_workers=cfg.MAX_PROCESS_FILE_THREAD,
            thread_name_prefix="scan_slot",
            initializer=partial(self._thread_worker_initializer, thread_local),
        ) as pool:
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
                canonical_curdir_path = Path(
                    replace_root(
                        delta_src_curdir_path,
                        self._delta_src_mount_point,
                        _canonical_root,
                    )
                )

                _dir_should_process, _dir_should_fully_scan = (
                    self._check_if_need_to_process_dir(canonical_curdir_path)
                )
                if not _dir_should_process:
                    dirnames.clear()
                    self._dirs_to_remove.add_path(canonical_curdir_path)
                    continue

                # skip files that over the max_filenum_per_folder,
                # and add these files to remove list
                if len(filenames) > self.MAX_FILENUM_PER_FOLDER:
                    logger.warning(
                        f"reach max_filenum_per_folder on {delta_src_curdir_path}, "
                        "exceeded files will be ignored silently"
                    )
                self._process_dir(
                    filenames[: self.MAX_FILENUM_PER_FOLDER],
                    _dir_should_fully_scan,
                    delta_src_curdir_path=delta_src_curdir_path,
                    canonical_curdir_path=canonical_curdir_path,
                    pool=pool,
                    thread_local=thread_local,
                )

        # heals the hole of the ft_rs table
        self._rst_orm.orm_execute("VACUUM;")

    def _cleanup_base(self):
        # NOTE: the dirs in dirs_to_remove is cannonical dirs!
        for _canon_dir in self._dirs_to_remove.iter_paths():
            _delta_src_dir = replace_root(
                _canon_dir, CANONICAL_ROOT, self._delta_src_mount_point
            )
            shutil.rmtree(_delta_src_dir)

    def process_slot(self):
        self._calculate_delta()
        self._cleanup_base()


class InplaceMode:

    def __init__(
        self,
        *,
        ota_metadata: OTAMetadata,
        standby_slot_mount_point: str,
        resource_dir: Path,
        status_report_queue: Queue[StatusReport],
        session_id: str,
    ) -> None:
        self._ota_metadata = ota_metadata
        self._status_report_queue = status_report_queue
        self.session_id = session_id

        self._standby_slot_mp = Path(standby_slot_mount_point)
        self._resource_dir = Path(resource_dir)

    def _preprocess_regular_file_entries(
        self,
    ) -> Generator[tuple[bytes, list[RegularFileEntry]]]:
        """Yield a group of regular file entries which have the same digest each time.

        NOTE: it depends on the regular file table is sorted by digest!
        """
        cur_digest_group: list[RegularFileEntry] = []
        cur_digest: bytes = b""
        for _entry in self._ota_metadata.iter_regular_entries():
            _this_digest = _entry.digest
            if not cur_digest:
                cur_digest = _this_digest
                cur_digest_group.append(_entry)
                continue

            if _this_digest != cur_digest:
                yield cur_digest, cur_digest_group

                cur_digest = _this_digest
                cur_digest_group = [_entry]
            else:
                cur_digest_group.append(_entry)

        # NOTE: remember to yield the last group
        yield cur_digest, cur_digest_group

    def _process_one_regular_files_group(
        self, _input: tuple[bytes, list[RegularFileTypedDict]]
    ) -> tuple[int, int]:
        """Process a group of regular_files with the same digest.

        Returns:
            A tuple of int, first is the processed files number, second is the sume of
                processed files' size.
        """
        digest, entries = _input
        try:
            # NOTE: the very first entry in the group must be prepared by local copy or
            #   download from remote, which both cases are recorded previously, so we minus one
            #   entry when calculating the processed_files_num and processed_files_size.
            processed_files_num = len(entries) - 1
            processed_files_size = processed_files_num * (entries[0]["size"] or 0)

            _rs = self._resource_dir / digest.hex()

            _hardlinked: defaultdict[int, list[RegularFileTypedDict]] = defaultdict(
                list
            )
            _normal: list[RegularFileTypedDict] = []

            for entry in entries:
                if entry["links_count"] is not None:
                    _hardlinked[entry["inode_id"]].append(entry)
                else:
                    _normal.append(entry)

            _first_one_prepared = False
            for entry in _normal:
                if not _first_one_prepared:
                    prepare_regular(
                        entry,
                        _rs,
                        target_mnt=self._standby_slot_mp,
                        prepare_method="hardlink",
                    )
                    _first_one_prepared = True
                else:
                    prepare_regular(
                        entry,
                        _rs,
                        target_mnt=self._standby_slot_mp,
                        prepare_method="copy",
                    )

            for _, entries in _hardlinked.items():
                _hardlink_first_one = entries.pop()
                if not _first_one_prepared:
                    prepare_regular(
                        _hardlink_first_one,
                        _rs,
                        target_mnt=self._standby_slot_mp,
                        prepare_method="hardlink",
                    )
                    _first_one_prepared = True
                else:
                    prepare_regular(
                        _hardlink_first_one,
                        _rs,
                        target_mnt=self._standby_slot_mp,
                        prepare_method="copy",
                    )

                for entry in entries:
                    prepare_regular(
                        entry,
                        _rs=replace_root(
                            _hardlink_first_one["path"],
                            cfg.CANONICAL_ROOT,
                            self._standby_slot_mp,
                        ),
                        target_mnt=self._standby_slot_mp,
                        prepare_method="hardlink",
                    )
            return processed_files_num, processed_files_size
        except Exception as e:
            burst_suppressed_logger.exception(f"failed to process {_input}: {e!r}")
            raise

    # main entries for processing each type of files.

    def _process_regular_files(
        self,
        *,
        batch_concurrency: int = PROCESS_FILES_CONCURRENCY,
        num_of_workers: int = PROCESS_FILES_WORKER,
    ) -> None:
        logger.info("star to process regular entries ...")
        _next_commit_before, _batch_cnt = 0, 0
        _merged_payload = UpdateProgressReport(
            operation=UpdateProgressReport.Type.APPLY_DELTA
        )

        with ThreadPoolExecutorWithRetry(
            max_concurrent=batch_concurrency,
            max_total_retry=cfg.CREATE_STANDBY_RETRY_MAX,
            max_workers=num_of_workers,
        ) as _mapper:
            for _done_count, _done in enumerate(
                _mapper.ensure_tasks(
                    func=self._process_one_regular_files_group,
                    iterable=self._preprocess_regular_file_entries(),
                )
            ):
                _now = int(time.time())
                if _done.exception():
                    # NOTE: failure logging is handled by logging_wrapper
                    continue

                _processed_files_num, _processed_files_size = _done.result()
                _merged_payload.processed_file_num += _processed_files_num
                _merged_payload.processed_file_size += _processed_files_size

                if (
                    _this_batch := _done_count // PROCESS_FILES_REPORT_BATCH
                ) > _batch_cnt or _now > _next_commit_before:
                    _next_commit_before = _now + PROCESS_FILES_REPORT_INTERVAL
                    _batch_cnt = _this_batch

                    self._status_report_queue.put_nowait(
                        StatusReport(
                            payload=_merged_payload,
                            session_id=self.session_id,
                        )
                    )
                    _merged_payload = UpdateProgressReport(
                        operation=UpdateProgressReport.Type.APPLY_DELTA
                    )

            # commit left-over items that cannot fill the batch
            self._status_report_queue.put_nowait(
                StatusReport(
                    payload=_merged_payload,
                    session_id=self.session_id,
                )
            )

    def _process_dir_entries(self) -> None:
        logger.info("start to process directory entries ...")
        for entry in self._ota_metadata.iter_dir_entries():
            try:
                prepare_dir(entry, target_mnt=self._standby_slot_mp)
            except Exception as e:
                burst_suppressed_logger.exception(f"failed to process {entry=}: {e!r}")
                raise

    def _process_non_regular_files(self) -> None:
        logger.info("start to process non-regular entries ...")
        for entry in self._ota_metadata.iter_non_regular_entries():
            try:
                prepare_non_regular(entry, target_mnt=self._standby_slot_mp)
            except Exception as e:
                burst_suppressed_logger.exception(f"failed to process {entry=}: {e!r}")
                raise

    # API

    def update_slot(self) -> None:
        self._process_dir_entries()
        self._process_non_regular_files()
        self._process_regular_files()

        # finally, cleanup the resource dir
        shutil.rmtree(self._resource_dir)
