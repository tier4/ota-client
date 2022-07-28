from __future__ import annotations

import typing
import otaclient_v2_pb2 as v2
from enum import Enum
from google.protobuf import message as _message
from typing import Any, ClassVar, Optional, Type


class _WrapperBase:
    """A proxy wrapper base that proxies all attrs from/to the
    wrapped proto class instance."""

    proto_class: ClassVar[Type[_message.Message]]
    data: _message.Message

    def __init__(self, *args, **kwargs):
        self.data = self.proto_class(*args, **kwargs)

    def __getattr__(self, __name: str) -> Any:
        if __name in ["data", "proto_class"]:
            return super().__getattribute__(__name)
        return getattr(self.data, __name)

    def __getitem__(self, __name: str) -> Any:
        return getattr(self.data, __name)

    def __setattr__(self, __name: str, __value: Any):
        if __name in ["data", "proto_class"]:
            super().__setattr__(__name, __value)
        else:
            setattr(self.data, __name, __value)

    def __setitem__(self, __key: str, __value: Any):
        setattr(self.data, __key, __value)


_StatusResponse = typing.cast(Type[v2.StatusResponse], _WrapperBase)
_StatusResponseEcu = typing.cast(Type[v2.StatusResponseEcu], _WrapperBase)
_Status = typing.cast(Type[v2.Status], _WrapperBase)
_StatusProgress = typing.cast(Type[v2.StatusProgress], _WrapperBase)
_UpdateRequest = typing.cast(Type[v2.UpdateRequest], _WrapperBase)
_UpdateRequestEcu = typing.cast(Type[v2.UpdateRequestEcu], _WrapperBase)


class MessageWrapperMixin:
    proto_class: ClassVar[Type[_message.Message]]
    data: _message.Message

    def export_pb(self):
        res = self.proto_class()
        res.CopyFrom(self.data)
        return res

    @classmethod
    def wrap(cls, _in: Optional[_message.Message] = None):
        if _in is not None and not isinstance(_in, cls.proto_class):
            raise ValueError(
                f"wrong input type, expect={cls.proto_class}, in={_in.__class__}"
            )

        res = cls()
        if _in is not None:
            res.data = _in
        return res

    def copy(self):
        """Copy the wrapped data to a new wrapper with CopyFrom."""
        _new_data = self.proto_class()
        _new_data.CopyFrom(self.data)
        return self.wrap(_new_data)


class StatusResponseWrapper(MessageWrapperMixin, _StatusResponse):
    proto_class = v2.StatusResponse
    data: v2.StatusResponse


class StatusResponseEcuWrapper(MessageWrapperMixin, _StatusResponseEcu):
    proto_class = v2.StatusResponseEcu
    data: v2.StatusResponseEcu


class StatusWrapper(MessageWrapperMixin, _Status):
    proto_class = v2.Status
    data: v2.Status


class StatusProgressWrapper(MessageWrapperMixin, _StatusProgress):
    proto_class = v2.StatusProgress
    data: v2.StatusProgress


class UpdateRequestEcuWrapper(MessageWrapperMixin, _UpdateRequestEcu):
    proto_class = v2.UpdateRequestEcu
    data: v2.UpdateRequestEcu


class UpdateRequestWrapper(MessageWrapperMixin, _UpdateRequest):
    proto_class = v2.UpdateRequest
    data: v2.UpdateRequest

    def find_request(self, ecu_id: str) -> Optional[UpdateRequestEcuWrapper]:
        for request_ecu in self.ecu:
            if request_ecu.ecu_id == ecu_id:
                return UpdateRequestEcuWrapper.wrap(request_ecu)


class EnumWrapperMixin:
    def export_pb(self):
        raise self.value  # type: ignore


class FailureTypeWrapper(EnumWrapperMixin, Enum):
    NO_FAILURE = v2.NO_FAILURE
    RECOVERABLE = v2.RECOVERABLE
    UNRECOVERABLE = v2.UNRECOVERABLE

    def to_str(self) -> str:
        return f"{self.value:0>1}"


class StatusOtaWrapper(EnumWrapperMixin, Enum):
    INITIALIZED = v2.INITIALIZED
    SUCCESS = v2.SUCCESS
    FAILURE = v2.FAILURE
    UPDATING = v2.UPDATING
    ROLLBACKING = v2.ROLLBACKING
    ROLLBACK_FAILURE = v2.ROLLBACK_FAILURE


class StatusProgressPhaseWrapper(EnumWrapperMixin, Enum):
    INITIAL = v2.INITIAL
    METADATA = v2.METADATA
    DIRECTORY = v2.DIRECTORY
    SYMLINK = v2.SYMLINK
    REGULAR = v2.REGULAR
    PERSISTENT = v2.PERSISTENT
    POST_PROCESSING = v2.POST_PROCESSING
