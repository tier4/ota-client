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
import time
from pathlib import Path
from queue import Queue

from ota_metadata.file_table._orm import FileSystemTableORM
from ota_metadata.file_table._table import FileSystemTable, HardlinkRegister
from ota_metadata.legacy.metadata import OTAMetadata
from otaclient._status_monitor import StatusReport, UpdateProgressReport
from otaclient.configs.cfg import cfg
from otaclient_common.retry_task_map import (
    ThreadPoolExecutorWithRetry,
)
from otaclient_common.typing import StrOrPath

logger = logging.getLogger(__name__)

PROCESS_FILES_BATCH = 1000
PROCESS_FILES_REPORT_INTERVAL = 1  # second

# TODO: NOTE: (20241206) be very careful and ensure that each boot controller implementation
#   do the logic of copying required files from /boot folder to actual boot partition!
# 1. grub: need to implement this logic.
# 2. jetson-cboot: wait for investigation.
# 3. jetson-uefi: wait for investigation.
# 4. rpi_boot: wait for investigation.


class RebuildMode:

    def __init__(
        self,
        *,
        standby_slot_mount_point: str,
        ota_metadata: OTAMetadata,
        # NOTE: resource_dir should be under the standby_slot_mount_point
        resource_dir: StrOrPath,
        status_report_queue: Queue[StatusReport],
        session_id: str,
    ) -> None:
        self._ota_metadata = ota_metadata
        self._status_report_queue = status_report_queue
        self.session_id = session_id

        self._standby_slot_mp = Path(standby_slot_mount_point)
        self._resource_dir = Path(resource_dir)

        _fs_table_conn = ota_metadata.connect_fstable(read_only=True)
        self._ft_orm = FileSystemTableORM(_fs_table_conn)

        self._hardlink_ctx = HardlinkRegister()

    def _process_entry(self, entry: FileSystemTable) -> FileSystemTable:
        entry.copy_to_target(
            target_mnt=self._standby_slot_mp,
            resource_dir=self._resource_dir,
            ctx=self._hardlink_ctx,
        )
        return entry

    def rebuild_standby(self) -> None:
        _next_commit_before, _batch_cnt = 0, 0
        _merged_payload = UpdateProgressReport(
            operation=UpdateProgressReport.Type.APPLY_DELTA
        )

        with ThreadPoolExecutorWithRetry(
            max_concurrent=cfg.MAX_CONCURRENT_PROCESS_FILE_TASKS,
            max_total_retry=cfg.CREATE_STANDBY_RETRY_MAX,
        ) as _mapper:
            try:
                for _done_count, _done in enumerate(
                    _mapper.ensure_tasks(
                        self._process_entry,
                        self._ft_orm.orm_select_all_entries(
                            batch_size=PROCESS_FILES_BATCH
                        ),
                    )
                ):
                    _now = time.time()
                    if _done.exception():
                        continue

                    entry = _done.result()
                    _merged_payload.processed_file_num += 1
                    _merged_payload.processed_file_size += entry.inode.size or 0

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
            except Exception as e:
                logger.error(f"failed to finish up file processing: {e!r}")
                raise
