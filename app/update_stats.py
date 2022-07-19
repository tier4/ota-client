import dataclasses
import time
from enum import Enum
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from queue import Empty, Queue
from threading import Event, Lock
from typing import Any, Dict, Generator, List

from app.configs import config as cfg


@dataclasses.dataclass
class OTAUpdateStats:
    total_regular_files: int = 0
    total_regular_file_size: int = 0
    regular_files_processed: int = 0
    files_processed_copy: int = 0
    files_processed_link: int = 0
    files_processed_download: int = 0
    file_size_processed_copy: int = 0
    file_size_processed_link: int = 0
    file_size_processed_download: int = 0
    elapsed_time_copy: int = 0  # ns
    elapsed_time_link: int = 0  # ns
    elapsed_time_download: int = 0  # ns
    errors_download: int = 0
    total_elapsed_time: int = 0

    def copy(self) -> "OTAUpdateStats":
        return dataclasses.replace(self)

    def export_as_dict(self) -> Dict[str, int]:
        """
        NOTE: convert elasped_time_<op> from nano-second to milli-second here
        """
        _copy = self.copy()
        _copy.elapsed_time_copy = _copy.elapsed_time_copy // 10**6
        _copy.elapsed_time_download = _copy.elapsed_time_download // 10**6
        _copy.elapsed_time_link = _copy.elapsed_time_link // 10**6

        return dataclasses.asdict(_copy)

    def __getitem__(self, _key: str) -> int:
        return getattr(self, _key)

    def __setitem__(self, _key: str, _value: int):
        setattr(self, _key, _value)


class RegProcessOperation(Enum):
    OP_UNSPECIFIC = "unspecific"
    OP_DOWNLOAD = "download"
    OP_COPY = "copy"
    OP_LINK = "link"

    @classmethod
    def is_valid_op(cls, _op: Any) -> bool:
        if isinstance(_op, cls):
            return _op != cls.OP_UNSPECIFIC

        if isinstance(_op, str):
            try:
                cls[_op]
                return True
            except KeyError:
                return False

        return False


@dataclasses.dataclass
class RegInfProcessedStats:
    """processed_list have dictionaries as follows:
    {"size": int}  # file size
    {"elapsed_ns": int}  # elapsed time in nano-seconds
    {"op": str}  # operation. "copy", "link" or "download"
    {"errors": int}  # number of errors that occurred when downloading.
    """

    op: RegProcessOperation = RegProcessOperation.OP_UNSPECIFIC
    size: int = 0
    elapsed_ns: int = 0
    errors: int = 0


class OTAUpdateStatsCollector:
    def __init__(self) -> None:
        self._lock = Lock()
        self._started = False
        self.store = OTAUpdateStats()

        self.collect_interval = cfg.STATS_COLLECT_INTERVAL
        self.terminated = Event()
        self._que: Queue[RegInfProcessedStats] = Queue()
        self._staging: List[RegInfProcessedStats] = []

    @contextmanager
    def _staging_changes(self) -> Generator[OTAUpdateStats, None, None]:
        """Acquire a staging storage for updating the slot atomically.

        NOTE: it should be only one collecter that calling this method!
        """
        staging_slot = self.store.copy()
        try:
            yield staging_slot
        finally:
            self.store = staging_slot

    ###### public API ######

    def start(self, *, restart=False):
        if restart and self._started:
            self.stop()

        with self._lock:
            self.clear()
            if not self._started:
                self._executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="update_stats_collector"
                )
                self._executor.submit(self.collector)

    def stop(self):
        with self._lock:
            if self._started:
                self.terminated.set()
                self._executor.shutdown()
                self._started = False

            self.clear()  # cleanup stats storage

    def clear(self):
        self.store = OTAUpdateStats()

    def set_total_regular_files(self, value: int):
        self.store.total_regular_files = value

    def get_snapshot(self) -> OTAUpdateStats:
        """Return a copy of statistics storage."""
        return self.store.copy()

    def get_snapshot_as_dist(self) -> Dict[str, Any]:
        return self.store.copy().export_as_dict()

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
                        _suffix = st.op.value
                        if RegProcessOperation.is_valid_op(_suffix):
                            staging_storage.regular_files_processed += 1
                            staging_storage.total_regular_file_size += st.size
                            staging_storage[f"files_processed_{_suffix}"] += 1
                            staging_storage[f"file_size_processed_{_suffix}"] += st.size
                            staging_storage[
                                f"elapsed_time_{_suffix}"
                            ] += st.elapsed_ns  # in nano-seconds

                            if _suffix == RegProcessOperation.OP_DOWNLOAD.value:
                                staging_storage[f"errors_{_suffix}"] += st.errors

                # cleanup already collected stats
                self._staging.clear()

    def wait_staging(self):
        """This method will block until the self._staging is empty."""
        while len(self._staging) > 0 or self._que.qsize() > 0:
            time.sleep(self.collect_interval)

        # sleep extra 3 intervals to ensure the result is recorded
        time.sleep(self.collect_interval * 3)
