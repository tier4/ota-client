from abc import abstractmethod
from pathlib import Path
from typing import Protocol

from app.ota_status import OTAStatusEnum

# fmt: off
class BootControllerProtocol(Protocol):
    @abstractmethod
    def get_ota_status(self) -> OTAStatusEnum: ...
    @abstractmethod
    def get_standby_slot_path(self) -> Path: ...
    @abstractmethod
    def pre_update(self): ...
    @abstractmethod
    def post_update(self): ...
    @abstractmethod
    def post_rollback(self): ...
    @abstractmethod
    def load_version(self) -> str: ...

# fmt: on
