from enum import unique, Enum
from typing import Optional
from app.proto import otaclient_v2_pb2 as v2


@unique
class OTAUpdatePhase(Enum):
    INITIAL = 0
    METADATA = 1
    DIRECTORY = 2
    SYMLINK = 3
    REGULAR = 4
    PERSISTENT = 5
    POST_PROCESSING = 6

    @classmethod
    def convert_to_v2_StatusProgressPhase(cls, _in) -> Optional[v2.StatusProgressPhase]:
        try:
            if isinstance(_in, cls):
                return getattr(v2.StatusProgressPhase, _in.name)
        except AttributeError:
            pass
