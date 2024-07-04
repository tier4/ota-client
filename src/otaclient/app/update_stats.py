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
"""Implementation of stats and progress tracking during OTA."""


from __future__ import annotations

import queue
import time
from enum import Enum, auto
from threading import Thread

from pydantic import BaseModel


class ProcessOperation(Enum):
    UNSPECIFIC = auto()
    # NOTE: PREPARE_LOCAL, DOWNLOAD_REMOTE and APPLY_DELTA are together
    #       counted as <processed_files_*>
    PREPARE_LOCAL_COPY = auto()
    DOWNLOAD_REMOTE_COPY = auto()
    APPLY_DELTA = auto()
    # for in-place update only
    APPLY_REMOVE_DELTA = auto()


class OperationRecord(BaseModel):
    op: ProcessOperation = ProcessOperation.UNSPECIFIC

    processed_file_num: int = 0
    processed_file_size: int = 0  # uncompressed processed file size
    # currently only for downloading operation
    errors: int = 0


class OTAUpdateStatsCollector:

    def __init__(self, *, collect_interval: int = 1) -> None:
        self.collect_interval = collect_interval
        self._queue: queue.Queue[OperationRecord] = queue.Queue()
        self._collector: Thread | None = None

        self._shutdown = False

        self._update_started_timestamp = int(time.time())
        self._delta_calculation_started_timestamp = 0
        self._delta_calculation_finished_timestamp = 0

        self._download_started_timestamp = 0
        self._download_finished_timestamp = 0

        self._apply_update_started_timestamp = 0
        self._apply_update_finished_timestamp = 0

        #
        # ------ exposed attributes ------ #
        #

        # ------ summary ------ #
        self.processed_files_num: int = 0
        self.processed_files_size: int = 0

        # ------ delta calculation stats ------ #
        self.delta_collected_files_num: int = 0
        self.delta_collected_files_size: int = 0

        # ------ download stats ------ #
        self.downloaded_files_num: int = 0
        self.downloaded_files_size: int = 0
        self.downloading_errors: int = 0

        # ------ apply update stats ------ #
        self.removed_files_num: int = 0
        self.apply_update_files_num: int = 0
        self.apply_update_files_size: int = 0

    @property
    def total_elapsed_time(self) -> int:
        return int(time.time()) - self._update_started_timestamp

    @property
    def delta_calculation_elapsed_time(self) -> int:
        if self._delta_calculation_started_timestamp == 0:
            return 0
        if self._delta_calculation_finished_timestamp == 0:
            return int(time.time()) - self._delta_calculation_started_timestamp
        return (
            self._delta_calculation_finished_timestamp
            - self._delta_calculation_started_timestamp
        )

    @property
    def download_elapsed_time(self) -> int:
        if self._download_finished_timestamp == 0:
            return 0
        if self._download_finished_timestamp == 0:
            return int(time.time()) - self._download_started_timestamp
        return self._download_finished_timestamp - self._download_started_timestamp

    @property
    def apply_update_elapsed_time(self) -> int:
        if self._apply_update_started_timestamp == 0:
            return 0
        if self._apply_update_finished_timestamp == 0:
            return int(time.time()) - self._apply_update_started_timestamp
        return (
            self._apply_update_finished_timestamp - self._apply_update_started_timestamp
        )

    def _stats_collector(self):
        while not self._shutdown:
            try:
                entry = self._queue.get_nowait()
            except queue.Empty:
                time.sleep(self.collect_interval)
                continue

            self.processed_files_num += entry.processed_file_num
            self.processed_files_size += entry.processed_file_size
            if entry.op == ProcessOperation.DOWNLOAD_REMOTE_COPY:
                self.downloading_errors += entry.errors

    # APIs

    def download_started(self) -> None:
        self._download_started_timestamp = int(time.time())

    def download_finished(self) -> None:
        self._download_finished_timestamp = int(time.time())

    def delta_calculation_started(self) -> None:
        self._delta_calculation_started_timestamp = int(time.time())

    def delta_calculation_finished(self) -> None:
        self._delta_calculation_finished_timestamp = int(time.time())

    def apply_update_started(self) -> None:
        self._apply_update_started_timestamp = int(time.time())

    def apply_update_finished(self) -> None:
        self._apply_update_finished_timestamp = int(time.time())

    def report_stat(self, stat: OperationRecord) -> None:
        self._queue.put_nowait(stat)

    def start_collector(self):
        self._collector = Thread(target=self._stats_collector, daemon=True)
        self._collector.start()

    def finish_collecting(self, *, wait_staging=True):
        if not self._collector:
            return  # the collector is not started!

        if wait_staging:  # ensure all reports are parsed
            while not self._queue.empty():
                time.sleep(self.collect_interval)
        self._shutdown = True
        self._collector.join()
