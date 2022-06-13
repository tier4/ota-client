import dataclasses
from contextlib import contextmanager
from threading import Lock
from typing import Any, Dict, Generator

from app.interface import OTAUpdateStatsCollectorProtocol


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
    elapsed_time_copy: int = 0
    elapsed_time_link: int = 0
    elapsed_time_download: int = 0
    errors_download: int = 0
    total_elapsed_time: int = 0

    def copy(self) -> "OTAUpdateStats":
        return dataclasses.replace(self)

    def export_as_dict(self) -> Dict[str, int]:
        return dataclasses.asdict(self)

    def __getitem__(self, _key: str) -> int:
        return getattr(self, _key)

    def __setitem__(self, _key: str, _value: int):
        setattr(self, _key, _value)


class OTAUpdateStatsCollector(OTAUpdateStatsCollectorProtocol):
    def __init__(self) -> None:
        self._lock = Lock()
        self._store = OTAUpdateStats()

    def get_snapshot(self) -> OTAUpdateStats:
        """Return a copy of statistics storage."""
        return self._store.copy()

    def get(self, _key: str) -> int:
        return self._store[_key]

    def set(self, _attr: str, _value: Any):
        """Set a single attr in the slot."""
        with self._lock:
            self._store[_attr] = _value

    def clear(self):
        self._store = OTAUpdateStats()

    @contextmanager
    def acquire_staging_storage(self) -> Generator[OTAUpdateStats, None, None]:
        """Acquire a staging storage for updating the slot atomically and thread-safely."""
        try:
            self._lock.acquire()
            staging_slot = self._store.copy()
            yield self._store.copy()
        finally:
            self._store = staging_slot
            self._lock.release()
