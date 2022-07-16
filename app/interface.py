from abc import abstractmethod
from threading import Event
from typing import Optional, Protocol, Type

from app.boot_control.interface import BootControllerProtocol
from app.create_standby.interface import StandbySlotCreatorProtocol
from app.proto import otaclient_v2_pb2 as v2


class OTAClientProtocol(Protocol):
    def __init__(
        self,
        *,
        boot_control_cls: Type[BootControllerProtocol],
        create_standby_cls: Type[StandbySlotCreatorProtocol],
        my_ecu_id: str = "",
    ) -> None:
        ...

    @abstractmethod
    def update(
        self,
        version: str,
        url_base: str,
        cookies_json: str,
        *,
        pre_update_event: Optional[Event] = None,
        post_update_event: Optional[Event] = None,
    ) -> None:
        ...

    @abstractmethod
    def rollback(self) -> None:
        ...

    @abstractmethod
    def status(self) -> v2.StatusResponseEcu:
        ...
