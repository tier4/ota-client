from __future__ import annotations
from dataclasses import dataclass

import typing
import otaclient_v2_pb2 as v2
from enum import Enum
from google.protobuf import message as _message
from typing import (
    Any,
    ClassVar,
    Generator,
    List,
    Optional,
    Protocol,
    Tuple,
    Type,
    Union,
)


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

    def __eq__(self, __o: object) -> bool:
        if isinstance(__o, self.__class__):
            return __o.data == self.data
        return False

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

    @classmethod
    def copy_from(cls, _in: _message.Message):
        """Copy and wrap input message into a new wrapper instance."""
        _new_data = cls.proto_class()
        _new_data.CopyFrom(_in)
        return cls.wrap(_new_data)

    def rewrap(self, _in: _message.Message):
        """Replace the underlaying wrapped data instance."""
        self.data = _in

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


class RollbackResponseEcu(_RollbackResponseEcu, MessageWrapperProtocol):
    proto_class = v2.RollbackResponseEcu
    data: v2.RollbackResponseEcu


class RollbackResponse(_RollbackResponse, MessageWrapperProtocol):
    proto_class = v2.RollbackResponse
    data: v2.RollbackResponse

    def iter_ecu(
        self,
    ) -> Generator[Tuple[str, FailureType, RollbackResponseEcu], None, None]:
        for _ecu in self.data.ecu:
            yield _ecu.ecu_id, FailureType(_ecu.result), RollbackResponseEcu.wrap(_ecu)

    def add_ecu(self, _response_ecu: RollbackResponseEcu):
        _ecu = typing.cast(v2.RollbackResponseEcu, _response_ecu.unwrap())
        self.data.ecu.append(_ecu)


## status API
class StatusProgress(_StatusProgress, MessageWrapperProtocol):
    proto_class = v2.StatusProgress
    data: v2.StatusProgress

    def get_snapshot(self) -> StatusProgress:
        _export = self.export_pb()
        return self.__class__.wrap(_export)


class Status(_Status, MessageWrapperProtocol):
    proto_class = v2.Status
    data: v2.Status

    def get_progress(self) -> StatusProgress:
        return StatusProgress.wrap(self.progress)

    def get_failure(self) -> Tuple[FailureType, str]:
        return FailureType(self.failure), self.failure_reason


class StatusRequest(_StatusRequest, MessageWrapperProtocol):
    proto_class = v2.StatusRequest
    data: v2.StatusRequest


class StatusResponseEcu(_StatusResponseEcu, MessageWrapperProtocol):
    proto_class = v2.StatusResponseEcu
    data: v2.StatusResponseEcu


class StatusResponse(_StatusResponse, MessageWrapperProtocol):
    proto_class = v2.StatusResponse
    data: v2.StatusResponse

    def iter_ecu_status(self) -> Generator[Tuple[str, FailureType, Status], None, None]:
        """
        Returns:
            A tuple of (<ecu_id>, <failure_type>, <status>)
        """
        for _ecu in self.data.ecu:
            yield _ecu.ecu_id, FailureType(_ecu.result), Status.wrap(_ecu.status)

    def add_ecu(self, _response_ecu: StatusResponseEcu):
        _ecu = typing.cast(v2.StatusResponseEcu, _response_ecu.unwrap())
        self.data.ecu.append(_ecu)

    def merge_from(self, _in: Union["StatusResponse", v2.StatusResponse]):
        if isinstance(_in, StatusResponse):
            _in = _in.unwrap()  # type: ignore

        # NOTE: available_ecu_ids will not be merged
        for _ecu in _in.ecu:
            self.data.ecu.append(_ecu)

    def get_ecu_status(self, ecu_id: str) -> Optional[Tuple[str, FailureType, Status]]:
        """
        Returns:
            A tuple of (<ecu_id>, <failure_type>, <status>)
        """
        for _ecu in self.data.ecu:
            if _ecu.ecu_id == ecu_id:
                return _ecu.ecu_id, FailureType(_ecu.result), Status.wrap(_ecu.status)

    def update_available_ecu_ids(self, _ecu_list: List[str]):
        self.data.available_ecu_ids.extend(_ecu_list)


## update API
class UpdateRequestEcu(_UpdateRequestEcu, MessageWrapperProtocol):
    proto_class = v2.UpdateRequestEcu
    data: v2.UpdateRequestEcu


class UpdateRequest(_UpdateRequest, MessageWrapperProtocol):
    proto_class = v2.UpdateRequest
    data: v2.UpdateRequest

    def find_update_meta(self, ecu_id: str) -> Optional[UpdateRequestEcu]:
        for _ecu in self.data.ecu:
            if _ecu.ecu_id == ecu_id:
                return UpdateRequestEcu.wrap(_ecu)

    def iter_update_meta(self) -> Generator[UpdateRequestEcu, None, None]:
        for _ecu in self.data.ecu:
            yield UpdateRequestEcu.wrap(_ecu)


class UpdateResponseEcu(_UpdateResponseEcu, MessageWrapperProtocol):
    proto_class = v2.UpdateResponseEcu
    data: v2.UpdateResponseEcu


class UpdateResponse(_UpdateResponse, MessageWrapperProtocol):
    proto_class = v2.UpdateResponse
    data: v2.UpdateResponse

    def iter_ecu(self) -> Generator[UpdateResponseEcu, None, None]:
        for _ecu in self.data.ecu:
            _wrapped = UpdateResponseEcu.wrap(_ecu)
            yield _wrapped

    def add_ecu(self, _response_ecu: UpdateResponseEcu):
        _ecu = typing.cast(v2.UpdateResponseEcu, _response_ecu.unwrap())
        self.data.ecu.append(_ecu)


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
