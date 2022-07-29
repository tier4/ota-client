from __future__ import annotations

import typing
import otaclient_v2_pb2 as v2
from enum import Enum
from google.protobuf import message as _message
from typing import Any, ClassVar, List, Optional, Protocol, Type


class _WrapperBase:
    """Base for wrapper types."""

    proto_class: ClassVar[Type[_message.Message]]

    def __init__(self, *args, **kwargs):
        self.data = self.proto_class(*args, **kwargs)


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


class MessageWrapperProtocol(Protocol):
    """A proxy wrapper base that proxies all attrs from/to the
    wrapped proto class instance."""

    proto_class: ClassVar[Type[_message.Message]]
    data: _message.Message

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


class EnumWrapper(Enum):
    def export_pb(self):
        return self.value  # type: ignore

    def __eq__(self, __o: object) -> bool:
        """Support directly comparing with v2 Enum types."""
        if isinstance(__o, self.__class__):
            return super().__eq__(__o)
        return self.value == __o


# message


## rollback
class RollbackRequestEcu(_RollbackRequestEcu, MessageWrapperProtocol):
    proto_class = v2.RollbackRequestEcu
    data: v2.RollbackRequestEcu


class RollbackRequest(_RollbackRequest, MessageWrapperProtocol):
    proto_class = v2.RollbackRequest
    data: v2.RollbackRequest

    def add_ecu(self, _response_ecu: RollbackRequestEcu):
        _ecu = typing.cast(v2.RollbackRequestEcu, _response_ecu.export_pb())
        self.ecu.append(_ecu)


class RollbackResponseEcu(_RollbackResponseEcu, MessageWrapperProtocol):
    proto_class = v2.RollbackResponseEcu
    data: v2.RollbackResponseEcu


class RollbackResponse(_RollbackResponse, MessageWrapperProtocol):
    proto_class = v2.RollbackResponse
    data: v2.RollbackResponse

    def add_ecu(self, _response_ecu: RollbackResponseEcu):
        _ecu = typing.cast(v2.RollbackResponseEcu, _response_ecu.export_pb())
        self.ecu.append(_ecu)


## status API
class Status(_Status, MessageWrapperProtocol):
    proto_class = v2.Status
    data: v2.Status


class StatusProgress(_StatusProgress, MessageWrapperProtocol):
    proto_class = v2.StatusProgress
    data: v2.StatusProgress


class StatusRequest(_StatusRequest, MessageWrapperProtocol):
    proto_class = v2.StatusRequest
    data: v2.StatusRequest


class StatusResponseEcu(_StatusResponseEcu, MessageWrapperProtocol):
    proto_class = v2.StatusResponseEcu
    data: v2.StatusResponseEcu


class StatusResponse(_StatusResponse, MessageWrapperProtocol):
    proto_class = v2.StatusResponse
    data: v2.StatusResponse

    def add_ecu(self, _response_ecu: StatusResponseEcu):
        _ecu = typing.cast(v2.StatusResponseEcu, _response_ecu.export_pb())
        self.ecu.append(_ecu)

    def update_available_ecu_ids(self, _ecu_list: List[str]):
        self.available_ecu_ids.extend(_ecu_list)


## update API
class UpdateRequestEcu(_UpdateRequestEcu, MessageWrapperProtocol):
    proto_class = v2.UpdateRequestEcu
    data: v2.UpdateRequestEcu


class UpdateRequest(_UpdateRequest, MessageWrapperProtocol):
    proto_class = v2.UpdateRequest
    data: v2.UpdateRequest

    def find_request(self, ecu_id: str) -> Optional[UpdateRequestEcu]:
        for request_ecu in self.ecu:
            if request_ecu.ecu_id == ecu_id:
                return UpdateRequestEcu.wrap(request_ecu)

    def add_ecu(self, _response_ecu: UpdateRequestEcu):
        _ecu = typing.cast(v2.UpdateRequestEcu, _response_ecu.export_pb())
        self.ecu.append(_ecu)


class UpdateResponseEcu(_UpdateResponseEcu, MessageWrapperProtocol):
    proto_class = v2.UpdateResponseEcu
    data: v2.UpdateResponseEcu


class UpdateResponse(_UpdateResponse, MessageWrapperProtocol):
    proto_class = v2.UpdateResponse
    data: v2.UpdateResponse

    def add_ecu(self, _response_ecu: UpdateResponseEcu):
        _ecu = typing.cast(v2.UpdateResponseEcu, _response_ecu.export_pb())
        self.ecu.append(_ecu)


# enum


class FailureType(EnumWrapper):
    NO_FAILURE = v2.NO_FAILURE
    RECOVERABLE = v2.RECOVERABLE
    UNRECOVERABLE = v2.UNRECOVERABLE

    def to_str(self) -> str:
        return f"{self.value:0>1}"


class StatusOta(EnumWrapper):
    INITIALIZED = v2.INITIALIZED
    SUCCESS = v2.SUCCESS
    FAILURE = v2.FAILURE
    UPDATING = v2.UPDATING
    ROLLBACKING = v2.ROLLBACKING
    ROLLBACK_FAILURE = v2.ROLLBACK_FAILURE


class StatusProgressPhase(EnumWrapper):
    INITIAL = v2.INITIAL
    METADATA = v2.METADATA
    DIRECTORY = v2.DIRECTORY
    SYMLINK = v2.SYMLINK
    REGULAR = v2.REGULAR
    PERSISTENT = v2.PERSISTENT
    POST_PROCESSING = v2.POST_PROCESSING
