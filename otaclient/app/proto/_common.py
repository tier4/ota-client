# Copyright 2022 TIER IV, INC. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from __future__ import annotations
import io
from abc import abstractmethod
from enum import Enum
from google.protobuf.message import Message as _Message
from google.protobuf.duration_pb2 import Duration as _Duration
from typing import cast, Any, List, Optional, Generic, Type, TypeVar, Generic

# NOTE: this typevar is expected to cover subtypes of protobuf Message type
_MessageType = TypeVar("_MessageType")


# converter base


class _ProtobufConverter(Generic[_MessageType]):
    """Base class for all message converter class."""

    proto_class: Type[_MessageType]

    @classmethod
    @abstractmethod
    def convert(cls, _in: _MessageType):
        raise NotImplementedError

    @abstractmethod
    def export_pb(self) -> _MessageType:
        raise NotImplementedError


# the converter mapping between protobuf message/container type and converter type


class TypeConverterRegister(Generic[_MessageType]):
    TYPE_CONVERTER_REGISTER = {}

    def __new__(cls, *args, **kwargs) -> None:
        raise ValueError("this class should not be instantiated")

    @classmethod
    def register_converter(
        cls, message_type: Type[_MessageType], converter_type: Type[_ProtobufConverter]
    ):
        if not message_type in cls.TYPE_CONVERTER_REGISTER:
            cls.TYPE_CONVERTER_REGISTER[message_type] = converter_type

    @classmethod
    def get_converter(
        cls, message_type: Type[_MessageType], default=None
    ) -> Optional[_ProtobufConverter]:
        return cls.TYPE_CONVERTER_REGISTER.get(message_type, default)


# helper functions


def _general_converter(_value: Any, _field_type=None) -> Any:
    """For message types that have corresponding converter,
    convert the message to wrapped version using the converter."""
    if not _field_type:
        _field_type = type(_value)
    if _converter := TypeConverterRegister.get_converter(_field_type):
        _value = _converter.convert(_value)
    # protobuf message type must have a converter
    elif isinstance(_field_type, _Message):
        raise NotImplementedError(
            f"converter for protobuf type {_field_type} is not implemented"
        )
    # for type without converter defined and not protobuf message type
    return _value


# well-known type converter
# check https://protobuf.dev/reference/python/python-generated/#wkt for details

# NOTE: only support Duration as we only use Duration currently.
# NOTE: concrete wrapper implementation should register the DurationWrapper
#       whenever Duration protobuf type is used.

_pb2_Duration = cast(Type[_Duration], object)


class DurationWrapper(_ProtobufConverter[_Duration], int, _pb2_Duration):  # type: ignore
    proto_class = _Duration

    @classmethod
    def from_int(cls, value: int) -> DurationWrapper:
        """Helper method to convert an int to this class' inst.

        NOTE: although we can directly use DurationWrapper(<value>), the
              static type checker will complain, so we use this helper
              method instead.
        """
        return cls(value)  # type: ignore

    @classmethod
    def convert(cls, _in: _Duration) -> DurationWrapper:
        return cls(_in.ToNanoseconds())  # type: ignore

    def export_pb(self) -> _Duration:
        (_res := self.proto_class()).FromNanoseconds(self)
        return _res


# container converter

# NOTE: currently only support repeated field.
# NOTE: concrete wrapper implementation should register the ListLikeContainerWrapper
#       with the type of the repeated field at runtime, as we cannot reliably detect
#       the actual protobuf type for repeated field statically.
#
#       (as for protoc==3.21.11, protobuf==4.21.12, pyi file will
#       type hint the repeated field as google.protobuf.internal.containers.RepeatedCompositeFieldContainer,
#       but in runtime google._upb._message.RepeatedCompositeContainer is used).
#       According to upb's document, its API/ABI should consider unstable and should
#       not be directly used.


class ListLikeContainerWrapper(_ProtobufConverter[List[Any]], list):
    proto_class = list  # NOTE: just a placeholder, don't use

    def __str__(self) -> str:
        _buffer = io.StringIO()
        _buffer.write("[")
        for _entry in self:
            _buffer.write(f"\n\t{str(_entry)},")
        _buffer.write("]")
        return _buffer.getvalue()

    @classmethod
    def convert(cls, _in: List[Any]) -> List[Any]:
        _self = cls()
        for _entry in _in:
            _self.append(_general_converter(_entry))
        return _self

    def export_pb(self) -> List[Any]:
        _res = []
        for _entry in self:
            if isinstance(_entry, _ProtobufConverter):
                _res.append(_entry.export_pb())
            else:
                _res.append(_entry)
        return _res


# message converter


class MessageWrapperBase(Generic[_MessageType]):
    """Base for message converter."""

    proto_class: Type[_MessageType]
    __slots__ = []  # all attrs of corresponding proto class should be registered here

    def __init__(self, *args, **kwargs):
        """Get all attrs from a protobuf class inst and then bind to this inst.

        NOTE: <args> is ignored, always use <kwargs>
        """
        for _key, _value in kwargs.items():
            if _key not in self.__slots__:
                continue
            setattr(self, _key, _value)

    def __str__(self) -> str:
        _buffer = io.StringIO()

        _buffer.write("{")
        for _attrn in self.__slots__:
            _buffer.write(f"\n\t{_attrn}: {getattr(self, _attrn)}")
        _buffer.write("\n}")
        return _buffer.getvalue()


class MessageWrapper(_ProtobufConverter, MessageWrapperBase[_MessageType]):
    proto_class: Type[_MessageType]
    __slots__ = []

    # public API

    @classmethod
    def convert(cls, _in: _MessageType):
        """Copy and wrap input message into a new wrapper instance."""
        if not isinstance(_in, cls.proto_class):
            raise TypeError(
                f"wrong input type, expect={cls.proto_class}, in={_in.__class__}"
            )

        _kwargs = {}
        # only capture the attrs presented in this wrapper class' __slots__
        for _attrn in cls.__slots__:
            try:
                _attrv = getattr(_in, _attrn)
                _kwargs[_attrn] = _general_converter(_attrv)
            except AttributeError:
                pass
        return cls(**_kwargs)

    def export_pb(self) -> _MessageType:
        """Export as protobuf message class inst."""
        _res = self.proto_class()
        for _key in self.__slots__:
            try:
                _value = getattr(self, _key)
            except AttributeError:
                continue

            # if _value is the converted version, export to pb first
            if isinstance(_value, _ProtobufConverter):
                # for message, we should use <CopyFrom> API to assign the value
                if issubclass(_value.proto_class, _Message):
                    getattr(_res, _key).CopyFrom(_value.export_pb())
                # for repeated field, use <extend> API to assign
                elif isinstance(_value, ListLikeContainerWrapper):
                    getattr(_res, _key).extend(_value.export_pb())
                else:  # for any other non-Message protobuf type
                    setattr(_res, _key, _value.export_pb())
            else:  # for attr in normal python type, directly assign
                setattr(_res, _key, _value)
        return _res


# enum type


class EnumWrapper(Enum):
    """Wrap protobuf defined enum into python Enum.

    NOTE: as for protoc==3.21.11, protobuf==4.21.12, at runtime the
          type of protobuf Enum value is int, the enum value itself
          is not the instance of any Enum type defined in protobuf
          package.
    """

    @classmethod
    def convert(cls, _in):
        return cls(_in.value)  # type: ignore

    def export_pb(self):
        return self.value

    def __eq__(self, __o: Any) -> bool:
        """Support directly eq comparing with protobuf Enum."""
        if isinstance(__o, self.__class__):
            return super().__eq__(__o)
        # unwrap first and then compare to protobuf Enum.
        return self.value == __o
