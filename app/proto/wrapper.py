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


_RollbackRequest = typing.cast(Type[v2.RollbackRequest], _WrapperBase)
_RollbackRequestEcu = typing.cast(Type[v2.RollbackRequestEcu], _WrapperBase)
_RollbackResponse = typing.cast(Type[v2.RollbackResponse], _WrapperBase)
_RollbackResponseEcu = typing.cast(Type[v2.RollbackResponseEcu], _WrapperBase)
_Status = typing.cast(Type[v2.Status], _WrapperBase)
_StatusProgress = typing.cast(Type[v2.StatusProgress], _WrapperBase)
_StatusRequest = typing.cast(Type[v2.StatusRequest], _WrapperBase)
_StatusResponse = typing.cast(Type[v2.StatusResponse], _WrapperBase)
_StatusResponseEcu = typing.cast(Type[v2.StatusResponseEcu], _WrapperBase)
_UpdateRequest = typing.cast(Type[v2.UpdateRequest], _WrapperBase)
_UpdateRequestEcu = typing.cast(Type[v2.UpdateRequestEcu], _WrapperBase)
_UpdateResponse = typing.cast(Type[v2.UpdateResponse], _WrapperBase)
_UpdateResponseEcu = typing.cast(Type[v2.UpdateRequestEcu], _WrapperBase)


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

    def unwrap(self):
        return self.data


class EnumWrapperMixin:
    __proto_class__: ClassVar[Type]

    def export_pb(self):
        return self.value  # type: ignore

    def __eq__(self, __o: object) -> bool:
        """Support directly comparing with v2 Enum types."""
        if isinstance(__o, self.__proto_class__):
            return __o == self.value  # type: ignore
        if isinstance(__o, self.__class__):
            return __o == self
        return False


# message


## rollback
class RollbackRequestEcu(MessageWrapperMixin, _RollbackRequestEcu):
    proto_class = v2.RollbackRequestEcu
    data: v2.RollbackRequestEcu


class RollbackRequest(MessageWrapperMixin, _RollbackRequest):
    proto_class = v2.RollbackRequest
    data: v2.RollbackRequest


class RollbackResponseEcu(MessageWrapperMixin, _RollbackResponseEcu):
    proto_class = v2.RollbackResponseEcu
    data: v2.RollbackResponseEcu


class RollbackResponse(MessageWrapperMixin, _RollbackResponse):
    proto_class = v2.RollbackResponse
    data: v2.RollbackResponse

    def add_ecu(self, _response_ecu: RollbackResponseEcu):
        _ecu = typing.cast(v2.RollbackResponseEcu, _response_ecu.unwrap())
        self.data.ecu.append(_ecu)


## status API
class Status(MessageWrapperMixin, _Status):
    proto_class = v2.Status
    data: v2.Status


class StatusProgress(MessageWrapperMixin, _StatusProgress):
    proto_class = v2.StatusProgress
    data: v2.StatusProgress


class StatusRequest(MessageWrapperMixin, _StatusRequest):
    proto_class = v2.StatusRequest
    data: v2.StatusRequest


class StatusResponseEcu(MessageWrapperMixin, _StatusResponseEcu):
    proto_class = v2.StatusResponseEcu
    data: v2.StatusResponseEcu


class StatusResponse(MessageWrapperMixin, _StatusResponse):
    proto_class = v2.StatusResponse
    data: v2.StatusResponse

    def add_ecu(self, _response_ecu: StatusResponseEcu):
        _ecu = typing.cast(v2.StatusResponseEcu, _response_ecu.unwrap())
        self.data.ecu.append(_ecu)


## update API
class UpdateRequestEcu(MessageWrapperMixin, _UpdateRequestEcu):
    proto_class = v2.UpdateRequestEcu
    data: v2.UpdateRequestEcu


class UpdateRequest(MessageWrapperMixin, _UpdateRequest):
    proto_class = v2.UpdateRequest
    data: v2.UpdateRequest

    def find_request(self, ecu_id: str) -> Optional[UpdateRequestEcu]:
        for request_ecu in self.ecu:
            if request_ecu.ecu_id == ecu_id:
                return UpdateRequestEcu.wrap(request_ecu)


class UpdateResponseEcu(MessageWrapperMixin, _UpdateResponseEcu):
    proto_class = v2.UpdateResponseEcu
    data: v2.UpdateResponseEcu


class UpdateResponse(MessageWrapperMixin, _UpdateResponse):
    proto_class = v2.UpdateResponse
    data: v2.UpdateResponse

    def add_ecu(self, _response_ecu: UpdateResponseEcu):
        _ecu = typing.cast(v2.UpdateResponseEcu, _response_ecu.unwrap())
        self.data.ecu.append(_ecu)


# enum


class FailureType(EnumWrapperMixin, Enum):
    __proto_class__ = v2.FailureType
    NO_FAILURE = v2.NO_FAILURE
    RECOVERABLE = v2.RECOVERABLE
    UNRECOVERABLE = v2.UNRECOVERABLE

    def to_str(self) -> str:
        return f"{self.value:0>1}"


class StatusOta(EnumWrapperMixin, Enum):
    __proto_class__ = v2.StatusOta
    INITIALIZED = v2.INITIALIZED
    SUCCESS = v2.SUCCESS
    FAILURE = v2.FAILURE
    UPDATING = v2.UPDATING
    ROLLBACKING = v2.ROLLBACKING
    ROLLBACK_FAILURE = v2.ROLLBACK_FAILURE


class StatusProgressPhase(EnumWrapperMixin, Enum):
    __proto_class__ = v2.StatusProgressPhase
    INITIAL = v2.INITIAL
    METADATA = v2.METADATA
    DIRECTORY = v2.DIRECTORY
    SYMLINK = v2.SYMLINK
    REGULAR = v2.REGULAR
    PERSISTENT = v2.PERSISTENT
    POST_PROCESSING = v2.POST_PROCESSING
