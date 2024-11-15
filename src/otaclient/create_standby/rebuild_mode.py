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

import logging
import os
import shutil
import time
from pathlib import Path
from queue import Queue

from ota_metadata.legacy.parser import MetafilesV1, OTAMetadata
from ota_metadata.legacy.types import RegularInf
from otaclient.configs.cfg import cfg
from otaclient._status_monitor import StatusReport, UpdateProgressReport
from otaclient_common.retry_task_map import (
    TasksEnsureFailed,
    ThreadPoolExecutorWithRetry,
)

from .common import DeltaBundle, DeltaGenerator, HardlinkRegister
from .interface import StandbySlotCreatorProtocol

logger = logging.getLogger(__name__)

PROCESS_FILES_BATCH = 1000
PROCESS_FILES_REPORT_INTERVAL = 1  # second


class RebuildMode(StandbySlotCreatorProtocol):
    def __init__(
        self,
        *,
        ota_metadata: OTAMetadata,
        boot_dir: str,
        standby_slot_mount_point: str,
        active_slot_mount_point: str,
        status_report_queue: Queue[StatusReport],
        session_id: str,
    ) -> None:
        self._ota_metadata = ota_metadata
        self._status_report_queue = status_report_queue
        self.session_id = session_id

        # path configuration
        self.boot_dir = Path(boot_dir)
        self.standby_slot_mp = Path(standby_slot_mount_point)
        self.active_slot_mp = Path(active_slot_mount_point)

        # recycle folder, files copied from referenced slot will be stored here,
        # also the meta files will be stored under this folder
        self._ota_tmp = self.standby_slot_mp / Path(cfg.OTA_TMP_STORE).relative_to("/")
        self._ota_tmp.mkdir(exist_ok=True)

    def _cal_and_prepare_delta(self):
        logger.info("generating delta...")
        delta_calculator = DeltaGenerator(
            ota_metadata=self._ota_metadata,
            delta_src=self.active_slot_mp,
            local_copy_dir=self._ota_tmp,
            status_report_queue=self._status_report_queue,
            session_id=self.session_id,
        )
        delta_bundle = delta_calculator.calculate_and_process_delta()
        logger.info(f"total_regular_files_num={delta_bundle.total_regular_num}")
        self.delta_bundle = delta_bundle

    def _process_dirs(self):
        for entry in self.delta_bundle.get_new_dirs():
            entry.mkdir_relative_to_mount_point(self.standby_slot_mp)

    def _process_symlinks(self):
        for _symlink in self._ota_metadata.iter_metafile(
            MetafilesV1.SYMBOLICLINK_FNAME
        ):
            _symlink.link_at_mount_point(self.standby_slot_mp)

    def _process_regulars(self):
        logger.info("start applying delta...")
        self._hardlink_register = HardlinkRegister()

        def _task(_item):
            try:
                return self._process_regular(_item)
            except Exception as e:
                logger.error(f"failed to process {_item}: {e!r}")
                raise

        with ThreadPoolExecutorWithRetry(
            max_concurrent=cfg.MAX_CONCURRENT_PROCESS_FILE_TASKS,
            max_total_retry=cfg.CREATE_STANDBY_RETRY_MAX,
        ) as _mapper:
            _next_commit_before, _batch_cnt = 0, 0
            _merged_payload = UpdateProgressReport(
                operation=UpdateProgressReport.Type.APPLY_DELTA
            )
            try:
                for _done_count, _done in enumerate(
                    _mapper.ensure_tasks(
                        _task,
                        self.delta_bundle.new_delta.items(),
                    )
                ):
                    _now = time.time()
                    if _done.exception():
                        continue

                    stat_report = _done.result()
                    _merged_payload.processed_file_num += stat_report.processed_file_num
                    _merged_payload.processed_file_size += (
                        stat_report.processed_file_size
                    )

                    if (
                        _this_batch := _done_count // PROCESS_FILES_BATCH
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
            except ValueError as e:
                logger.error(f"failed to start file process threadpool: {e!r}")
                raise
            except TasksEnsureFailed as e:
                logger.error(f"failed to finish up file processing: {e!r}")
                raise

    def _process_regular(
        self, _input: tuple[bytes, set[RegularInf]]
    ) -> UpdateProgressReport:
        _hash, _regs_set = _input
        _hash_str = _hash.hex()
        stat_report = UpdateProgressReport(
            operation=UpdateProgressReport.Type.APPLY_DELTA
        )

        _local_copy = self._ota_tmp / _hash_str
        _f_size = _local_copy.stat().st_size
        for _count, entry in enumerate(_regs_set, start=1):
            is_last = _count == len(_regs_set)

            # special treatment on /boot folder
            _mount_point = (
                self.standby_slot_mp
                if not entry.path.startswith("/boot")
                else self.boot_dir
            )

            # prepare this entry
            # case 1: normal file
            if entry.nlink == 1:
                if is_last:  # move the tmp entry to the dst
                    entry.move_from_src(_local_copy, dst_mount_point=_mount_point)
                else:  # copy from the tmp dir
                    entry.copy_from_src(_local_copy, dst_mount_point=_mount_point)
            # case 2: hardlink file
            else:
                # NOTE(20220523): for regulars.txt that support hardlink group,
                #   use inode to identify the hardlink group.
                #   otherwise, use hash to identify the same hardlink file.
                _identifier = entry.sha256hash if not entry.inode else entry.inode

                _dst = entry.relatively_join(_mount_point)
                _hardlink_tracker, _is_writer = self._hardlink_register.get_tracker(
                    _identifier, _dst, entry.nlink
                )

                logger.debug(f"hardlink file({_is_writer=}): {entry=}")
                if _is_writer:
                    entry.copy_from_src(_local_copy, dst_mount_point=_mount_point)
                else:  # subscriber
                    _src = _hardlink_tracker.subscribe_no_wait()
                    os.link(_src, _dst)

                if is_last:
                    _local_copy.unlink(missing_ok=True)

            # NOTE(20240704): the first copy of the file is either prepared by local delta copy
            #   or downloading from remote, so unconditionally pop one record away.
            if not is_last:
                stat_report.processed_file_num += 1
                stat_report.processed_file_size += _f_size

        return stat_report

    def _save_meta(self):
        """Save metadata to META_FOLDER."""
        _dst = self.standby_slot_mp / Path(cfg.IMAGE_META_DPATH).relative_to(
            cfg.CANONICAL_ROOT
        )
        _dst.mkdir(parents=True, exist_ok=True)

        logger.info(f"save image meta files to {_dst}")
        self._ota_metadata.save_metafiles_bin_to(_dst)

    # API

    @classmethod
    def should_erase_standby_slot(cls) -> bool:
        return True

    def calculate_and_prepare_delta(self) -> DeltaBundle:
        self._cal_and_prepare_delta()
        return self.delta_bundle

    def create_standby_slot(self):
        """Apply changes to the standby slot.

        This method should be called before calculate_and_prepare_delta.
        """
        self._process_dirs()
        self._process_regulars()
        self._process_symlinks()
        # NOTE(20240219): mv persist file handling logic from
        #                 create_standby implementation to post_update hook.
        # self._process_persistents()
        self._save_meta()
        # cleanup on successful finish
        # NOTE: if failed, the tmp dirs will not be removed now,
        #       but rebuild mode will not reuse the tmp dirs anyway
        #       as it requires standby_slot to be erased before applying
        #       the changes.
        shutil.rmtree(self._ota_tmp, ignore_errors=True)
