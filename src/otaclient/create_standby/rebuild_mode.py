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
from typing import Generator

from ota_metadata.file_table._orm import (
    FileTableNonRegularFilesORM,
    FileTableRegularFilesORM,
)
from ota_metadata.file_table._table import (
    FileTableNonRegularFiles,
    FileTableRegularFiles,
)
from ota_metadata.legacy.metadata import OTAMetadata
from otaclient._status_monitor import StatusReport, UpdateProgressReport
from otaclient.configs.cfg import cfg
from otaclient_common.retry_task_map import (
    ThreadPoolExecutorWithRetry,
)
from otaclient_common.typing import StrOrPath

logger = logging.getLogger(__name__)

PROCESS_FILES_REPORT_BATCH = 256
PROCESS_FILES_REPORT_INTERVAL = 1  # second


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

        # NOTE: at this point the regular_files file table should have been sorted by digest
        self._ft_regulars_orm = FileTableRegularFilesORM(
            ota_metadata.connect_fstable(read_only=True)
        )
        self._ft_non_regulars_orm = FileTableNonRegularFilesORM(
            ota_metadata.connect_fstable(read_only=True)
        )

    def _iter_regular_file_entries(
        self, *, batch_size: int
    ) -> Generator[tuple[bytes, list[FileTableRegularFiles]], None, None]:
        """Yield a group of regular file entries which have the same digest each time.

        NOTE: it depends on the regular file table is sorted by digest!
        """
        cur_digest_group: list[FileTableRegularFiles] = []
        cur_digest: bytes = b""
        for _entry in self._ft_regulars_orm.iter_all(batch_size=batch_size):
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
        # remember the last group
        yield cur_digest, cur_digest_group

    def _process_one_non_regular_file(self, entry: FileTableNonRegularFiles) -> None:
        return entry.prepare_target(target_mnt=self._standby_slot_mp)

    def _process_one_regular_files_group(
        self, _input: tuple[bytes, list[FileTableRegularFiles]]
    ) -> tuple[int, int]:
        """Process a group of regular_files with the same digest.

        Returns:
            A tuple of int, first is the processed files number, second is the sume of
                processed files' size.
        """
        digest, entries = _input

        # NOTE: the very first entry in the group must be prepared by local copy or
        #   download from remote, which both cases are recorded previously, so we minus one
        #   entry when calculating the processed_files_num and processed_files_size.
        processed_files_num = len(entries) - 1
        processed_files_size = processed_files_num * (entries[0].inode.size or 0)

        _rs = self._resource_dir / digest.hex()

        _hardlinked: dict[int, list[FileTableRegularFiles]] = {}
        _normal: list[FileTableRegularFiles] = []
        for entry in entries:
            if (_inode_group := entry.is_hardlink()) is not None:
                _entries_list = _hardlinked.setdefault(_inode_group, [])
                _entries_list.append(entry)
            else:
                _normal.append(entry)

        _first_one_prepared = False
        for entry in _normal:
            if not _first_one_prepared:
                entry.prepare_target(
                    _rs, target_mnt=self._standby_slot_mp, prepare_method="hardlink"
                )
                _first_one_prepared = True
            else:
                entry.prepare_target(
                    _rs, target_mnt=self._standby_slot_mp, prepare_method="copy"
                )

        for _, entries in _hardlinked.items():
            _hardlink_first_one = entries.pop()
            if not _first_one_prepared:
                _hardlink_first_one.prepare_target(
                    _rs, target_mnt=self._standby_slot_mp, prepare_method="hardlink"
                )
                _first_one_prepared = True
            else:
                _hardlink_first_one.prepare_target(
                    _rs, target_mnt=self._standby_slot_mp, prepare_method="copy"
                )

            _hardlink_first_one_fpath = _hardlink_first_one.fpath_on_target(
                target_mnt=self._standby_slot_mp
            )
            for entry in entries:
                entry.prepare_target(
                    _hardlink_first_one_fpath,
                    target_mnt=self._standby_slot_mp,
                    prepare_method="hardlink",
                )

        # finally, remove the resource. Note that if anything wrong happens,
        #   the _rs will not be removed on purpose.
        _rs.unlink(missing_ok=True)

        return processed_files_num, processed_files_size

    def _process_regular_files(
        self, *, batch_size: int = cfg.MAX_CONCURRENT_PROCESS_FILE_TASKS
    ) -> None:
        with ThreadPoolExecutorWithRetry(
            max_concurrent=batch_size,
            max_total_retry=cfg.CREATE_STANDBY_RETRY_MAX,
        ) as _mapper:
            _next_commit_before, _batch_cnt = 0, 0
            _merged_payload = UpdateProgressReport(
                operation=UpdateProgressReport.Type.APPLY_DELTA
            )

            try:
                for _done_count, _done in enumerate(
                    _mapper.ensure_tasks(
                        func=self._process_one_regular_files_group,
                        iterable=self._iter_regular_file_entries(batch_size=batch_size),
                    )
                ):
                    _now = int(time.time())
                    if _exc := _done.exception():
                        logger.warning(
                            f"file process failure detected: {_exc!r}, still retrying ..."
                        )
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
            except Exception as e:
                logger.error(f"failed to finish up file processing: {e!r}")
                raise

    def _process_non_regular_files(
        self, *, batch_size: int = cfg.MAX_CONCURRENT_PROCESS_FILE_TASKS
    ) -> None:
        with ThreadPoolExecutorWithRetry(
            max_concurrent=batch_size,
            max_total_retry=cfg.CREATE_STANDBY_RETRY_MAX,
        ) as _mapper:
            try:
                for _, _done in enumerate(
                    _mapper.ensure_tasks(
                        func=self._process_one_non_regular_file,
                        iterable=self._ft_non_regulars_orm.iter_all(
                            batch_size=batch_size
                        ),
                    )
                ):
                    if _exc := _done.exception():
                        logger.warning(
                            f"file process failure detected: {_exc!r}, still retrying ..."
                        )
                        continue
            except Exception as e:
                logger.error(f"failed to finish up file processing: {e!r}")
                raise

    # API

    def rebuild_standby(self) -> None:
        try:
            self._process_non_regular_files()
            self._process_regular_files()
        finally:
            self._ft_non_regulars_orm.orm_con.close()
            self._ft_regulars_orm.orm_con.close()
