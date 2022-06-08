from abc import abstractmethod
from email.generator import Generator
from enum import Enum, unique, auto
from pathlib import Path
from typing import Any, Protocol, TypeVar


# fmt: off
@unique
class OtaStatus(Enum):
    INITIALIZED = 0
    SUCCESS = auto()
    FAILURE = auto()
    UPDATING = auto()
    ROLLBACKING = auto()
    ROLLBACK_FAILURE = auto()

class OTAStatusHandlerProtocol(Protocol):
    standby_slot_status_file: Path
    current_slot_status_file: Path

    @abstractmethod
    def init_ota_status(self): ...
    @abstractmethod
    def get_current_ota_status(self) -> OtaStatus: ...
    @abstractmethod
    def get_live_ota_status(self) -> OtaStatus: ...
    @abstractmethod
    def store_standby_ota_status(self, _status: OtaStatus): ...
    @abstractmethod
    def set_live_ota_status(self, _status: OtaStatus): ...
    @abstractmethod
    def request_update(self) -> None: ...
    @abstractmethod
    def request_rollback(self) -> None: ...

_STATS = TypeVar("_STATS")

class OTAUpdateStatsCollectorProtocol(Protocol[_STATS]):
    @abstractmethod
    def get_snapshot(self) -> _STATS: ...
    @abstractmethod
    def set(self, _key: str, _value: Any): ...
    @abstractmethod
    def get(self, _key: str) -> Any: ...
    @abstractmethod
    def clear(self): ...
    @abstractmethod
    def staging_changes(self) -> Generator[_STATS, None, None]: ...

# fmt: on
