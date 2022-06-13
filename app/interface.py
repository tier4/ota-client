from abc import ABC, abstractmethod
from email.generator import Generator
from threading import Event
from typing import Any, Protocol, TypeVar

from app.ota_status import OTAStatusEnum


# fmt: off
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

class OTAClientInterface(ABC):
    def update(
        self, 
        version: str, 
        url_base: str, 
        cookies_json: str, 
        *, 
        pre_update_event: Event = None, post_update_event: Event = None) -> Any: ...

    def rollback(self) -> Any: ...
    def status(self) -> Any: ...

    def get_ota_status(self) -> OTAStatusEnum: ...
    def set_ota_status(self, _status: OTAStatusEnum): ...
    def request_update(self) -> None: 
        """Check whether current ota_status is legal for applying an OTA update."""
    def request_rollback(self) -> None:
        """Check whether current ota_status is legal for applying an OTA rollback."""

# fmt: on
