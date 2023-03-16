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


import time
from dataclasses import dataclass
from enum import Enum
from contextlib import contextmanager
from queue import Empty, Queue
from threading import Event, Lock, Thread
from typing import Generator, List

from . import log_setting
from .configs import config as cfg
from .proto.wrapper import UpdateStatus


logger = log_setting.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class RegProcessOperation(Enum):
    UNSPECIFIC = "UNSPECIFIC"
    # NOTE: PREPARE_LOCAL, DOWNLOAD_REMOTE and APPLY_DELTA are together
    #       counted as <processed_files_*>
    PREPARE_LOCAL_COPY = "PREPARE_LOCAL_COPY"
    DOWNLOAD_REMOTE_COPY = "DOWNLOAD_REMOTE_COPY"
    APPLY_DELTA = "APPLY_DELTA"
    # for in-place update only
    APPLY_REMOVE_DELTA = "APPLY_REMOVE_DELTA"
    # special op for download error report
    DOWNLOAD_ERROR_REPORT = "DOWNLOAD_ERROR_REPORT"


@dataclass
class RegInfProcessedStats:
    op: RegProcessOperation = RegProcessOperation.UNSPECIFIC

    size: int = 0  # uncompressed processed file size
    elapsed_ns: int = 0
    # only for downloading operation
    download_errors: int = 0
    downloaded_bytes: int = 0


class OTAUpdateStatsCollector:
    def __init__(self) -> None:
        self._lock = Lock()
        self.store = UpdateStatus()

        self.collect_interval = cfg.STATS_COLLECT_INTERVAL
        self.terminated = Event()
        self._que: Queue[RegInfProcessedStats] = Queue()
        self._staging: List[RegInfProcessedStats] = []
        self._collector_thread = None

    @contextmanager
    def _staging_changes(self) -> Generator[UpdateStatus, None, None]:
        """Acquire a staging storage for updating the slot atomically.

        NOTE: it should be only one collecter that calling this method!
        """
        staging_slot = self.store.get_snapshot()
        try:
            yield staging_slot
        finally:
            self.store = staging_slot

    def _clear(self):
        self.store = UpdateStatus()
        self._staging.clear()
        self._que = Queue()

    ###### public API ######

    def start(self):
        if not self.terminated.is_set():
            self.stop()

        with self._lock:
            if self.terminated.is_set():
                self.terminated.clear()
                self._collector_thread = Thread(target=self.collector)
                self._collector_thread.start()
            else:
                logger.warning("detect active collector, abort lauching new collector")

    def stop(self):
        with self._lock:
            if not self.terminated.is_set():
                self.terminated.set()
                if self._collector_thread is not None:
                    # wait for the collector thread to stop
                    self._collector_thread.join()
                    self._collector_thread = None
                # cleanup stats storage
                self._clear()

    def get_snapshot(self) -> UpdateStatus:
        """Return a copy of statistics storage."""
        return self.store.get_snapshot()

    def report(self, *stats: RegInfProcessedStats, op: RegProcessOperation):
        # NOTE: for APPLY_DELTA operation,
        #       unconditionally pop one stat from the stats_list
        #       because the preparation of first copy is already recorded
        #       (either by picking up local copy(keep_delta) or downloading)
        if op is RegProcessOperation.APPLY_DELTA:
            stats = stats[1:]
        for _stat in stats:
            self._que.put_nowait(_stat)

    def collector(self):
        _prev_time = time.time()
        while self._staging or not self.terminated.is_set():
            if not self.terminated.is_set():
                try:
                    _sts = self._que.get_nowait()
                    self._staging.append(_sts)
                except Empty:
                    # if no new stats available, wait <_interval> time
                    time.sleep(self.collect_interval)

            _cur_time = time.time()
            if self._staging and _cur_time - _prev_time >= self.collect_interval:
                _prev_time = _cur_time
                with self._staging_changes() as staging_storage:
                    for st in self._staging:
                        _op = st.op
                        if _op == RegProcessOperation.DOWNLOAD_REMOTE_COPY:
                            # update download specific fields
                            staging_storage.downloaded_bytes += st.downloaded_bytes
                            staging_storage.downloaded_files_num += 1
                            staging_storage.downloaded_files_size += st.size
                            staging_storage.downloading_errors += st.download_errors
                            staging_storage.downloading_elapsed_time.add_nanoseconds(
                                st.elapsed_ns
                            )
                            # as remote_delta, update processed_files_*
                            staging_storage.processed_files_num += 1
                            staging_storage.processed_files_size += st.size
                        elif _op == RegProcessOperation.DOWNLOAD_ERROR_REPORT:
                            staging_storage.downloading_errors += st.download_errors
                        elif _op == RegProcessOperation.PREPARE_LOCAL_COPY:
                            # update delta generating specific fields
                            staging_storage.delta_generating_elapsed_time.add_nanoseconds(
                                st.elapsed_ns
                            )
                            # as keep_delta, update processed_files_*
                            staging_storage.processed_files_size += st.size
                            staging_storage.processed_files_num += 1
                        elif _op == RegProcessOperation.APPLY_REMOVE_DELTA:
                            staging_storage.removed_files_num += 1
                        elif _op == RegProcessOperation.APPLY_DELTA:
                            # as applying_delta, update processed_files_*
                            staging_storage.processed_files_num += 1
                            staging_storage.processed_files_size += st.size
                            staging_storage.update_applying_elapsed_time.add_nanoseconds(
                                st.elapsed_ns
                            )
                # cleanup already collected stats
                self._staging.clear()

    def wait_staging(self):
        """This method will block until the self._staging is empty."""
        while len(self._staging) > 0 or self._que.qsize() > 0:
            time.sleep(self.collect_interval)

        # sleep extra 3 intervals to ensure the result is recorded
        time.sleep(self.collect_interval * 3)
