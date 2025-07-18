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
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue

from ota_metadata.file_table.utils import (
    RegularFileTypedDict,
    prepare_dir,
    prepare_non_regular,
    prepare_regular,
)
from ota_metadata.legacy2.metadata import OTAMetadata
from otaclient._status_monitor import StatusReport, UpdateProgressReport
from otaclient.configs.cfg import cfg
from otaclient.create_standby.delta_gen import UpdateStandbySlotFailed
from otaclient_common._logging import get_burst_suppressed_logger

logger = logging.getLogger(__name__)
burst_suppressed_logger = get_burst_suppressed_logger(f"{__name__}.file_op_failed")


class UpdateStandbySlot:
    def __init__(
        self,
        *,
        ota_metadata: OTAMetadata,
        standby_slot_mount_point: str,
        resource_dir: Path,
        status_report_queue: Queue[StatusReport],
        session_id: str,
        status_report_interval: int = cfg.PROCESS_FILES_REPORT_INTERVAL,
        max_workers: int = 5,
        concurrent_tasks: int = 1024,
    ) -> None:
        self.status_report_interval = status_report_interval
        self._ota_metadata = ota_metadata
        self._status_report_queue = status_report_queue
        self.session_id = session_id
        self._que: Queue[tuple[bytes, list[RegularFileTypedDict]] | None] = Queue()

        self._standby_slot_mp = Path(standby_slot_mount_point)
        self._resource_dir = Path(resource_dir)

        self.max_workers = max_workers
        self._se = threading.Semaphore(concurrent_tasks)
        self._interrupted = threading.Event()
        self._thread_local = threading.local()

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

    def _process_one_files_group_workload(
        self, cur_digest: bytes, entries: list[RegularFileTypedDict]
    ) -> None:
        hardlink_group: dict[int, Path] = {}
        cur_resource = None
        try:
            _merged_payload: UpdateProgressReport = self._thread_local.merged_payload
            for entry in entries:
                if self._interrupted.is_set():
                    burst_suppressed_logger.warning(
                        "workload skip on interrupted flag set, abort"
                    )
                    return

                _inode_id = entry["inode_id"]
                _size = entry["size"] or 0
                _is_hardlinked = entry["links_count"] is not None

                # NOTE that first copy will not be counted in report, as the first copy
                #   is either prepared by download, or by local, both are already recorded.
                if cur_resource is None:
                    cur_resource = prepare_regular(
                        entry,
                        _rs=self._resource_dir / cur_digest.hex(),
                        target_mnt=self._standby_slot_mp,
                        prepare_method="move",
                    )
                    if _is_hardlinked:
                        hardlink_group[_inode_id] = cur_resource
                # not the first copy in this digest group
                elif _is_hardlinked:
                    _merged_payload.processed_file_num += 1
                    _merged_payload.processed_file_size += _size

                    # hardlinked entry shared the same inode, thus same permissions
                    if _inode_id in hardlink_group:
                        prepare_regular(
                            entry,
                            _rs=hardlink_group[_inode_id],
                            target_mnt=self._standby_slot_mp,
                            prepare_method="hardlink",
                            hardlink_skip_apply_permission=True,
                        )
                    else:  # first entry in a hardlink group
                        hardlink_group[_inode_id] = prepare_regular(
                            entry,
                            _rs=cur_resource,
                            target_mnt=self._standby_slot_mp,
                            prepare_method="copy",
                        )
                # normal multi copies
                else:
                    _merged_payload.processed_file_num += 1
                    _merged_payload.processed_file_size += _size

                    prepare_regular(
                        entry,
                        cur_resource,
                        target_mnt=self._standby_slot_mp,
                        prepare_method="copy",
                    )

            # check if we need to do report after processing this group
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
        except Exception as e:
            burst_suppressed_logger.exception(
                f"file entries group process failed: {e!r}"
            )
            self._interrupted.set()
        finally:
            self._se.release()

    def _process_regular_file_entries(self) -> None:
        """Dispatch a group of regular file entries to thread pool which
            have the same digest each time.

        NOTE: it depends on the regular file table is sorted by digest!
        """
        logger.info("process regular file entries ...")
        with ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="ota_update_slot",
            initializer=self._worker_initializer,
        ) as pool:
            cur_digest: bytes = b""
            cur_entries: list[RegularFileTypedDict] = []

            for _entry in self._ota_metadata.iter_regular_entries():
                if self._interrupted.is_set():
                    logger.error("detect worker failed, abort!")
                    return

                _this_digest = _entry["digest"]
                if _this_digest != cur_digest:
                    if cur_entries:
                        self._se.acquire()
                        pool.submit(
                            self._process_one_files_group_workload,
                            cur_digest,
                            cur_entries,
                        )
                    cur_digest = _this_digest
                    cur_entries = [_entry]
                else:
                    cur_entries.append(_entry)

            # remember the final group
            if cur_entries:
                self._se.acquire()
                pool.submit(
                    self._process_one_files_group_workload, cur_digest, cur_entries
                )

            # finalizing all the workers
            barrier = threading.Barrier(self.max_workers + 1)
            for _ in range(self.max_workers):
                pool.submit(self._worker_finalizer, barrier)
            barrier.wait()

    def _process_dir_entries(self) -> None:
        logger.info("start to process directory entries ...")
        for entry in self._ota_metadata.iter_dir_entries():
            try:
                prepare_dir(entry, target_mnt=self._standby_slot_mp)
            except Exception as e:
                logger.exception(f"failed to process {dict(entry)=}: {e!r}")
                raise UpdateStandbySlotFailed(
                    f"failed to process {entry=}: {e!r}"
                ) from e

    def _process_non_regular_files(self) -> None:
        logger.info("start to process non-regular entries ...")
        for entry in self._ota_metadata.iter_non_regular_entries():
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

        # finally, cleanup the resource dir
        shutil.rmtree(self._resource_dir, ignore_errors=True)
