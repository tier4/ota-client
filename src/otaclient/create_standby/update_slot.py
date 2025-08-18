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
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from queue import Queue

from ota_metadata.file_table.db import FileTableDBHelper
from ota_metadata.file_table.utils import (
    RegularFileTypedDict,
    prepare_dir,
    prepare_non_regular,
    prepare_regular_copy,
    prepare_regular_hardlink,
    prepare_regular_inlined,
)
from otaclient._status_monitor import StatusReport, UpdateProgressReport
from otaclient.configs.cfg import cfg
from otaclient.create_standby.delta_gen import UpdateStandbySlotFailed
from otaclient_common._logging import get_burst_suppressed_logger
from otaclient_common.thread_safe_container import ThreadSafeDict

logger = logging.getLogger(__name__)
burst_suppressed_logger = get_burst_suppressed_logger(f"{__name__}.file_op_failed")


class UpdateStandbySlot:
    def __init__(
        self,
        *,
        file_table_db_helper: FileTableDBHelper,
        standby_slot_mount_point: str,
        resource_dir: Path,
        status_report_queue: Queue[StatusReport],
        session_id: str,
        status_report_interval: int = cfg.PROCESS_FILES_REPORT_INTERVAL,
        max_workers: int = 5,
        concurrent_tasks: int = 1024,
    ) -> None:
        self.status_report_interval = status_report_interval
        self._fst_db_helper = file_table_db_helper
        self._status_report_queue = status_report_queue
        self.session_id = session_id
        self._que: Queue[tuple[bytes, list[RegularFileTypedDict]] | None] = Queue()

        self._standby_slot_mp = Path(standby_slot_mount_point)
        self._resource_dir = Path(resource_dir)

        self.max_workers = max_workers
        self._se = threading.Semaphore(concurrent_tasks)
        self._interrupted = threading.Event()
        self._thread_local = threading.local()

        self._hardlink_group = ThreadSafeDict[int, Path]()

    def _worker_initializer(self):
        self._thread_local.merged_payload = UpdateProgressReport(
            operation=UpdateProgressReport.Type.APPLY_DELTA
        )
        self._thread_local.next_push = 0

    def _worker_finalizer(self, post_workload_barrier: threading.Barrier) -> None:
        """Let the thread worker push the final batch of report when finished."""
        _merged_payload: UpdateProgressReport = self._thread_local.merged_payload
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=_merged_payload,
                session_id=self.session_id,
            )
        )
        post_workload_barrier.wait()

    def _task_done_cb(self, _fut: Future):
        self._se.release()  # release se first
        if _exc := _fut.exception():
            burst_suppressed_logger.error(
                f"failure during processing: {_exc}", exc_info=_exc
            )
            self._interrupted.set()

    def _process_hardlinked_file_at_thread(
        self, _digest_hex: str, _entry: RegularFileTypedDict, first_to_prepare: bool
    ):
        _inode_id = _entry["inode_id"]
        _inlined = _entry["contents"] or _entry["size"] == 0
        with self._hardlink_group:
            _link_group_head = self._hardlink_group.get(_inode_id)
            if _link_group_head is not None:
                prepare_regular_hardlink(
                    _entry,
                    _rs=_link_group_head,
                    target_mnt=self._standby_slot_mp,
                    hardlink_skip_apply_permission=True,
                )
                self._post_regular_file_process(_entry)
                return

            if first_to_prepare:
                if _inlined:
                    self._hardlink_group[_inode_id] = prepare_regular_inlined(
                        _entry, target_mnt=self._standby_slot_mp
                    )
                    self._post_regular_file_process(_entry)
                else:
                    self._hardlink_group[_inode_id] = prepare_regular_hardlink(
                        _entry,
                        _rs=self._resource_dir / _digest_hex,
                        target_mnt=self._standby_slot_mp,
                    )
                return

            self._hardlink_group[_inode_id] = prepare_regular_copy(
                _entry,
                _rs=self._resource_dir / _digest_hex,
                target_mnt=self._standby_slot_mp,
            )
            self._post_regular_file_process(_entry)

    def _process_normal_file_at_thread(
        self, _digest_hex: str, _entry: RegularFileTypedDict, first_to_prepare: bool
    ):
        _inlined = _entry["contents"] or _entry["size"] == 0
        if _inlined:
            prepare_regular_inlined(_entry, target_mnt=self._standby_slot_mp)
            self._post_regular_file_process(_entry)
            return

        if first_to_prepare:
            prepare_regular_hardlink(
                _entry,
                _rs=self._resource_dir / _digest_hex,
                target_mnt=self._standby_slot_mp,
            )
        else:
            prepare_regular_copy(
                _entry,
                _rs=self._resource_dir / _digest_hex,
                target_mnt=self._standby_slot_mp,
            )
            self._post_regular_file_process(_entry)

    def _post_regular_file_process(self, _entry: RegularFileTypedDict):
        _merged_payload: UpdateProgressReport = self._thread_local.merged_payload
        _merged_payload.processed_file_num += 1
        _merged_payload.processed_file_size += _entry["size"]

        _now = time.perf_counter()
        if _now > self._thread_local.next_push:
            self._status_report_queue.put_nowait(
                StatusReport(
                    payload=_merged_payload,
                    session_id=self.session_id,
                )
            )
            self._thread_local.next_push = _now + self.status_report_interval
            self._thread_local.merged_payload = UpdateProgressReport(
                operation=UpdateProgressReport.Type.APPLY_DELTA
            )

    def _process_regular_file_entries(self) -> None:
        """Dispatch a group of regular file entries to thread pool which
            have the same digest each time.

        NOTE: it depends on the regular file table is sorted by digest!
        """
        logger.info("process regular file entries ...")
        _first_prepared_digest: set[bytes] = set()

        with ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="ota_update_slot",
            initializer=self._worker_initializer,
        ) as pool:
            for _entry in self._fst_db_helper.iter_regular_entries():
                if self._interrupted.is_set():
                    logger.error("detect worker failed, abort!")
                    return
                self._se.acquire()

                _digest = _entry["digest"]
                _digest_hex = _digest.hex()

                _first_to_prepare = False
                if _digest not in _first_prepared_digest:
                    _first_to_prepare = True
                    _first_prepared_digest.add(_digest)

                _links_count = _entry["links_count"]
                if _links_count is not None and _links_count > 1:
                    pool.submit(
                        self._process_hardlinked_file_at_thread,
                        _digest_hex,
                        _entry,
                        _first_to_prepare,
                    ).add_done_callback(self._task_done_cb)
                else:
                    pool.submit(
                        self._process_normal_file_at_thread,
                        _digest_hex,
                        _entry,
                        _first_to_prepare,
                    ).add_done_callback(self._task_done_cb)

            # finalizing all the workers
            barrier = threading.Barrier(self.max_workers + 1)
            for _ in range(self.max_workers):
                pool.submit(self._worker_finalizer, barrier)
            barrier.wait()

    def _process_dir_entries(self) -> None:
        logger.info("start to process directory entries ...")
        for entry in self._fst_db_helper.iter_dir_entries():
            try:
                prepare_dir(entry, target_mnt=self._standby_slot_mp)
            except Exception as e:
                logger.exception(f"failed to process {dict(entry)=}: {e!r}")
                raise UpdateStandbySlotFailed(
                    f"failed to process {entry=}: {e!r}"
                ) from e

    def _process_non_regular_files(self) -> None:
        logger.info("start to process non-regular entries ...")
        for entry in self._fst_db_helper.iter_non_regular_entries():
            try:
                prepare_non_regular(entry, target_mnt=self._standby_slot_mp)
            except Exception as e:
                logger.exception(f"failed to process {dict(entry)=}: {e!r}")
                raise UpdateStandbySlotFailed(
                    f"failed to process {entry=}: {e!r}"
                ) from e

    # API

    def update_slot(self) -> None:
        """
        Raises:
            UpdateStandbySlotFailed: if any error occurs during the process.
        """
        self._process_dir_entries()
        self._process_non_regular_files()
        self._process_regular_file_entries()

        if self._interrupted.is_set():
            raise UpdateStandbySlotFailed("failure during regular files processing!")
