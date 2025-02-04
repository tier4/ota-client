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
from functools import partial
from pathlib import Path
from queue import Queue
from typing import Callable, Generator, TypeVar

from typing_extensions import ParamSpec

from ota_metadata.file_table._table import (
    FileTableDirectories,
    FileTableNonRegularFiles,
    FileTableRegularFiles,
)
from ota_metadata.legacy2.metadata import OTAMetadata
from otaclient._status_monitor import StatusReport, UpdateProgressReport
from otaclient.configs.cfg import cfg
from otaclient_common.logging import BurstSuppressFilter
from otaclient_common.retry_task_map import (
    ThreadPoolExecutorWithRetry,
)
from otaclient_common.typing import StrOrPath

logger = logging.getLogger(__name__)
burst_suppressed_logger = logging.getLogger(f"{__name__}.process_error")
# NOTE: for request_error, only allow max 6 lines of logging per 30 seconds
burst_suppressed_logger.addFilter(
    BurstSuppressFilter(
        f"{__name__}.process_error",
        upper_logger_name=__name__,
        burst_round_length=30,
        burst_max=6,
    )
)

P = ParamSpec("P")
RT = TypeVar("RT")


def _failed_task_logging_wrapper(_func: Callable[P, RT]) -> Callable[P, RT]:
    def _wrapped(*args: P.args, **kwargs: P.kwargs) -> RT:
        try:
            return _func(*args, **kwargs)
        except Exception as e:
            burst_suppressed_logger.exception(
                f"failed to process ({args}, {kwargs}): {e!r}"
            )
            raise

    return _wrapped


PROCESS_FILES_REPORT_BATCH = 256
PROCESS_FILES_REPORT_INTERVAL = 1  # second

PROCESS_DIRS_BATCH_SIZE = 32

PROCESS_NON_REGULAR_FILES_BATCH_SIZE = 128
PROCESS_NON_REGULAR_FILES_CONCURRENCY = 18
PROCESS_NON_REGULAR_FILES_WORKER = 3

PROCESS_FILES_BATCH_SIZE = 256
PROCESS_FILES_CONCURRENCY = 64
PROCESS_FILES_WORKER = cfg.MAX_PROCESS_FILE_THREAD  # 6


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

    def _preprocess_regular_file_entries(
        self, *, batch_size: int
    ) -> Generator[tuple[bytes, list[FileTableRegularFiles]]]:
        """Yield a group of regular file entries which have the same digest each time.

        NOTE: it depends on the regular file table is sorted by digest!
        """
        cur_digest_group: list[FileTableRegularFiles] = []
        cur_digest: bytes = b""
        for _entry in self._ota_metadata.iter_regular_entries(batch_size=batch_size):
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

    def _process_one_regular_files_group(  # NOSONAR
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
        processed_files_size = processed_files_num * (entries[0].entry_attrs.size or 0)

        _rs = self._resource_dir / digest.hex()

        _hardlinked: dict[int, list[FileTableRegularFiles]] = {}
        _normal: list[FileTableRegularFiles] = []

        for entry in entries:
            if (_inode_group := entry.entry_attrs.inode) is not None:
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

    # main entries for processing each type of files.

    def _process_regular_files(
        self,
        *,
        batch_size: int = PROCESS_NON_REGULAR_FILES_BATCH_SIZE,
        batch_concurrency: int = PROCESS_FILES_CONCURRENCY,
        num_of_workers: int = PROCESS_FILES_WORKER,
    ) -> None:
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
                    func=_failed_task_logging_wrapper(
                        self._process_one_regular_files_group
                    ),
                    iterable=self._preprocess_regular_file_entries(
                        batch_size=batch_size
                    ),
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

    def _process_dir_entries(
        self,
        *,
        batch_size: int = PROCESS_DIRS_BATCH_SIZE,
    ) -> None:
        logger.info("process directories ...")
        _func = partial(
            FileTableDirectories.prepare_target,
            target_mnt=self._standby_slot_mp,
        )

        for entry in self._ota_metadata.iter_dir_entries(batch_size=batch_size):
            try:
                _func(entry)
            except Exception as e:
                burst_suppressed_logger.exception(f"failed to process {entry=}: {e!r}")
                raise

    def _process_non_regular_files(
        self,
        *,
        batch_size: int = PROCESS_NON_REGULAR_FILES_BATCH_SIZE,
        batch_cuncurrency: int = PROCESS_NON_REGULAR_FILES_CONCURRENCY,
        num_of_workers: int = PROCESS_NON_REGULAR_FILES_WORKER,
    ) -> None:
        logger.info("process non regular file entries ...")
        with ThreadPoolExecutorWithRetry(
            max_concurrent=batch_cuncurrency,
            max_total_retry=cfg.CREATE_STANDBY_RETRY_MAX,
            max_workers=num_of_workers,
        ) as _mapper:
            for _ in _mapper.ensure_tasks(
                func=_failed_task_logging_wrapper(
                    partial(
                        FileTableNonRegularFiles.prepare_target,
                        target_mnt=self._standby_slot_mp,
                    )
                ),
                iterable=self._ota_metadata.iter_non_regular_entries(
                    batch_size=batch_size
                ),
            ):
                """failure logging is handled by logging_wrapper."""

    # API

    def rebuild_standby(self) -> None:
        self._process_dir_entries()
        self._process_non_regular_files()
        self._process_regular_files()
