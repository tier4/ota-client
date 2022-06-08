from abc import abstractmethod
from enum import Enum, unique, auto
from pathlib import Path
from typing import Protocol


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

# fmt: on
