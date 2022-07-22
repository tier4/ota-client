from abc import abstractmethod
from pathlib import Path
from typing import Protocol

from app.ota_status import OTAStatusEnum


class BootControllerProtocol(Protocol):
    """Boot controller protocol for otaclient."""

    @abstractmethod
    def get_ota_status(self) -> OTAStatusEnum:
        """Get the stored ota_status of current active slot."""

    @abstractmethod
    def get_standby_slot_path(self) -> Path:
        """Get the Path points to the standby slot mount point."""

    @abstractmethod
    def get_standby_boot_dir(self) -> Path:
        """Get the Path points to the standby boot folder."""

    @abstractmethod
    def pre_update(self, version: str, *, standby_as_ref: bool, erase_standby: bool):
        ...

    @abstractmethod
    def pre_rollback(self):
        ...

    @abstractmethod
    def post_update(self):
        ...

    @abstractmethod
    def post_rollback(self):
        ...

    @abstractmethod
    def load_version(self) -> str:
        """Read the version info from the current slot."""

    @abstractmethod
    def on_operation_failure(self):
        ...
