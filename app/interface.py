from abc import ABC
from threading import Event
from typing import Any

# fmt: off

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

# fmt: on
