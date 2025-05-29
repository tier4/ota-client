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
from otaclient.create_standby.delta_gen import (
    PROCESS_FILES_REPORT_BATCH,
    PROCESS_FILES_REPORT_INTERVAL,
    UpdateStandbySlotFailed,
)
from otaclient_common import replace_root
from otaclient_common._logging import BurstSuppressFilter

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


class UpdateStandbySlot:
    def __init__(
        self,
        *,
        ota_metadata: OTAMetadata,
        standby_slot_mount_point: str,
        resource_dir: Path,
        status_report_queue: Queue[StatusReport],
        session_id: str,
        file_process_max_failure: int = cfg.CREATE_STANDBY_RETRY_MAX,
    ) -> None:
        self._ota_metadata = ota_metadata
        self._status_report_queue = status_report_queue
        self.session_id = session_id
        self._que: Queue[tuple[bytes, list[RegularFileTypedDict]] | None] = Queue()

        self._standby_slot_mp = Path(standby_slot_mount_point)
        self._resource_dir = Path(resource_dir)

        self._file_process_interrupted = threading.Event()
        self.file_process_max_failure = file_process_max_failure

    def _process_file_groups_thread_worker(self):
        """files group process worker."""
        _failure_count = 0

        _next_commit_before, _batch_cnt = 0, 0
        _merged_payload = UpdateProgressReport(
            operation=UpdateProgressReport.Type.APPLY_DELTA
        )

        _total_cnt = 0
        while _input := self._que.get():
            digest, entries = _input
            # NOTE: the very first entry in the group must be prepared by local copy or
            #   download from remote, which both cases are recorded previously, so we minus one
            #   entry when calculating the processed_files_num and processed_files_size.
            _total_cnt += 1
            processed_files_num = len(entries) - 1
            _merged_payload.processed_file_num += processed_files_num
            _merged_payload.processed_file_size += processed_files_num * (
                entries[0]["size"] or 0
            )

            resource_file = self._resource_dir / digest.hex()
            hardlinked: dict[int, str] = {}

            try:
                first_entry = entries.pop()
                prepare_regular(
                    first_entry,
                    _rs=resource_file,
                    target_mnt=self._standby_slot_mp,
                    prepare_method="hardlink",
                )
                if first_entry["links_count"] is not None:
                    hardlinked[first_entry["inode_id"]] = first_entry["path"]

                for entry in entries:
                    if entry["links_count"] is not None:
                        _inode_id = entry["inode_id"]
                        if _first_hardlinked_canon := hardlinked.get(_inode_id):
                            prepare_regular(
                                entry,
                                _rs=replace_root(
                                    _first_hardlinked_canon,
                                    cfg.CANONICAL_ROOT,
                                    self._standby_slot_mp,
                                ),
                                target_mnt=self._standby_slot_mp,
                                prepare_method="hardlink",
                            )
                        else:
                            prepare_regular(
                                entry,
                                resource_file,
                                target_mnt=self._standby_slot_mp,
                                prepare_method="copy",
                            )
                            hardlinked[_inode_id] = entry["path"]
                    else:
                        prepare_regular(
                            entry,
                            resource_file,
                            target_mnt=self._standby_slot_mp,
                            prepare_method="copy",
                        )

                _now = int(time.time())
                if (
                    _this_batch := _total_cnt // PROCESS_FILES_REPORT_BATCH
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

                # reset failure count at success process
                _failure_count = 0
            except BaseException as e:  # NOSONAR
                _failure_count += 1
                burst_suppressed_logger.exception(f"failed to process {_input}: {e!r}")

                if _failure_count > self.file_process_max_failure:
                    logger.error(
                        f"file process worker reaches maximum failure count({self.file_process_max_failure})!"
                        f"last failure: {e!r}"
                    )
                    self._file_process_interrupted.set()
                    self._que.put_nowait(None)  # stop other workers
                    return  # and then directly exit

                time.sleep(0.1)  # put the failure item back to que
                self._que.put_nowait(_input)

        # commit left-over items that cannot fill the batch
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=_merged_payload,
                session_id=self.session_id,
            )
        )
        # wait up other threads
        self._que.put_nowait(None)

    def _process_regular_file_entries(self) -> None:
        """Yield a group of regular file entries which have the same digest each time.

        NOTE: it depends on the regular file table is sorted by digest!
        """
        logger.info("start to process regular file entries ...")
        _workers: list[threading.Thread] = []
        for _ in range(cfg.MAX_PROCESS_FILE_THREAD):
            _t = threading.Thread(target=self._process_file_groups_thread_worker)
            _t.start()
            _workers.append(_t)

        try:
            cur_digest_group: list[RegularFileTypedDict] = []
            cur_digest: bytes = b""
            for _entry in self._ota_metadata.iter_regular_entries():
                if self._file_process_interrupted.is_set():
                    return

                _this_digest = _entry["digest"]
                if not cur_digest:
                    cur_digest = _this_digest
                    cur_digest_group.append(_entry)
                    continue

                if _this_digest != cur_digest:
                    self._que.put_nowait((cur_digest, cur_digest_group))

                    cur_digest = _this_digest
                    cur_digest_group = [_entry]
                else:
                    cur_digest_group.append(_entry)
            # NOTE: remember to yield the last group
            self._que.put_nowait((cur_digest, cur_digest_group))
        except Exception as e:
            logger.exception(f"itering file table database failed: {e!r}")
            raise UpdateStandbySlotFailed(
                f"itering file table database failed: {e!r}"
            ) from e
        finally:
            self._que.put_nowait(None)
            for _t in _workers:
                _t.join()

            # finally, check if any workers exit
            if self._file_process_interrupted.is_set():
                logger.error("not all workers finish work successfully")
                raise UpdateStandbySlotFailed(
                    "not all workers finish work successfully"
                )

    def _process_dir_entries(self) -> None:
        logger.info("start to process directory entries ...")
        for entry in self._ota_metadata.iter_dir_entries():
            try:
                prepare_dir(entry, target_mnt=self._standby_slot_mp)
            except Exception as e:
                burst_suppressed_logger.exception(f"failed to process {entry=}: {e!r}")
                raise UpdateStandbySlotFailed(
                    f"failed to process {entry=}: {e!r}"
                ) from e

    def _process_non_regular_files(self) -> None:
        logger.info("start to process non-regular entries ...")
        for entry in self._ota_metadata.iter_non_regular_entries():
            try:
                prepare_non_regular(entry, target_mnt=self._standby_slot_mp)
            except Exception as e:
                burst_suppressed_logger.exception(f"failed to process {entry=}: {e!r}")
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
        shutil.rmtree(self._resource_dir)
