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


import dataclasses
import time
from enum import Enum
from contextlib import contextmanager
from queue import Empty, Queue
from threading import Event, Lock, Thread
from typing import Generator, List

from .configs import config as cfg
from .proto import wrapper

from . import log_setting

logger = log_setting.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class RegProcessOperation(str, Enum):
    OP_UNSPECIFIC = "unspecific"
    OP_DOWNLOAD = "download"
    OP_COPY = "copy"
    OP_LINK = "link"
    # NOTE: failed download task will also be recorded, under the
    #       op_type==OP_DOWNLOAD_FAILED_REPORT.
    OP_DOWNLOAD_FAILED_REPORT = "download_failed_report"


@dataclasses.dataclass
class RegInfProcessedStats:
    """processed_list have dictionaries as follows:
    general fields:
        {"size": int}  # processed file size
        {"elapsed_ns": int}  # elapsed time in nano-seconds
        {"op": str}  # operation. "copy", "link" or "download"
    dedicated to download op
        {"errors": int}  # number of errors that occurred when downloading.
        {"download_bytes": int} # actual download size
    """

    op: RegProcessOperation = RegProcessOperation.OP_UNSPECIFIC
    size: int = 0
    elapsed_ns: int = 0
    errors: int = 0
    download_bytes: int = 0


class OTAUpdateStatsCollector:
    def __init__(self) -> None:
        self._lock = Lock()
        self.store = wrapper.StatusProgress()

        self.collect_interval = cfg.STATS_COLLECT_INTERVAL
        self.terminated = Event()
        self._que: Queue[RegInfProcessedStats] = Queue()
        self._staging: List[RegInfProcessedStats] = []
        self._collector_thread = None

    @contextmanager
    def _staging_changes(self) -> Generator[wrapper.StatusProgress, None, None]:
        """Acquire a staging storage for updating the slot atomically.

        NOTE: it should be only one collecter that calling this method!
        """
        staging_slot = self.store.get_snapshot()
        try:
            yield staging_slot
        finally:
            self.store = staging_slot

    def _clear(self):
        self.store = wrapper.StatusProgress()
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

    def set_total_regular_files(self, value: int):
        self.store.total_regular_files = value

    def set_total_regular_files_size(self, value: int):
        self.store.total_regular_file_size = value

    def get_snapshot(self) -> wrapper.StatusProgress:
        """Return a copy of statistics storage."""
        return self.store.get_snapshot()

    def report(self, *stats: RegInfProcessedStats):
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
                        if st.op in [
                            RegProcessOperation.OP_COPY,
                            RegProcessOperation.OP_DOWNLOAD,
                            RegProcessOperation.OP_LINK,
                        ]:
                            _suffix = st.op
                            staging_storage.regular_files_processed += 1
                            staging_storage[f"files_processed_{_suffix}"] += 1
                            staging_storage[f"file_size_processed_{_suffix}"] += st.size
                            staging_storage.add_elapsed_time(
                                f"elapsed_time_{_suffix}", st.elapsed_ns
                            )  # in nano-seconds
                            if st.op == RegProcessOperation.OP_DOWNLOAD:
                                staging_storage[
                                    f"errors_{RegProcessOperation.OP_DOWNLOAD.value}"
                                ] += st.errors
                                staging_storage.download_bytes += st.download_bytes
                        elif st.op == RegProcessOperation.OP_DOWNLOAD_FAILED_REPORT:
                            staging_storage[
                                f"errors_{RegProcessOperation.OP_DOWNLOAD.value}"
                            ] += st.errors
                # cleanup already collected stats
                self._staging.clear()

    def wait_staging(self):
        """This method will block until the self._staging is empty."""
        while len(self._staging) > 0 or self._que.qsize() > 0:
            time.sleep(self.collect_interval)

        # sleep extra 3 intervals to ensure the result is recorded
        time.sleep(self.collect_interval * 3)
