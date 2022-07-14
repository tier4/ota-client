from abc import ABC
from threading import Event
from typing import Any, Optional


class OTAClientInterface(ABC):
    def update(
        self,
        version: str,
        url_base: str,
        cookies_json: str,
        *,
        pre_update_event: Optional[Event] = None,
        post_update_event: Optional[Event] = None,
    ) -> Any:
        ...

    def rollback(self) -> Any:
        ...

    def status(self) -> Any:
        ...
