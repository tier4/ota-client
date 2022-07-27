from __future__ import annotations

import typing
import otaclient_v2_pb2 as v2
from enum import Enum
from google.protobuf import message as _message
from typing import Any, Type

# only for type hints
_UpdateRequest = typing.cast(Type[v2.UpdateRequest], object)


class MessageWrapperMixin:
    _proto_class: Type[_message.Message]
    data: _message.Message

    def export(self):
        res = self._proto_class()
        res.CopyFrom(self)  # type: ignore
        return res

    def __getattr__(self, __name: str) -> Any:
        """Proxy attributes from the wrapped UpdateRequest."""
        return getattr(self.data, __name)


class UpdateRequestWrapper(MessageWrapperMixin, _UpdateRequest):
    def __init__(self, _in: v2.UpdateRequest) -> None:
        self._proto_class = _in.__class__
        self.data = _in


class EnumWrapperMixin:
    @classmethod
    def export(cls, _in: Enum):
        if isinstance(_in, cls):
            return _in.value
        raise ValueError


class StatusOtaWrapper(EnumWrapperMixin, Enum):
    INITIALIZED = v2.INITIALIZED
    SUCCESS = v2.SUCCESS
    FAILURE = v2.FAILURE
    UPDATING = v2.UPDATING
    ROLLBACKING = v2.ROLLBACKING
    ROLLBACK_FAILURE = v2.ROLLBACK_FAILURE


class StatusProgressPhase(EnumWrapperMixin, Enum):
    INITIAL = v2.INITIAL
    METADATA = v2.METADATA
    DIRECTORY = v2.DIRECTORY
    SYMLINK = v2.SYMLINK
    REGULAR = v2.REGULAR
    PERSISTENT = v2.PERSISTENT
    POST_PROCESSING = v2.POST_PROCESSING
