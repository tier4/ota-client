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
from google.protobuf.message import Message as _Message
from google.protobuf.duration_pb2 import Duration as _Duration
from typing import (
    cast,
    overload,
    Any,
    List,
    Optional,
    Generic,
    Type,
    TypeVar,
    Generic,
)
from typing_extensions import Self

_MessageType = TypeVar("_MessageType")

# helper method


def _general_converter(_value: Any, _field_type=None) -> Any:
    """For message types that have corresponding converter,
    convert the message to wrapped version using the converter."""
    if not _field_type:
        _field_type = type(_value)
    # if converter for this type is available, always use it
    if _converter := TypeConverterRegister.get_converter(_field_type):
        _value = _converter.convert(_value)
    # protobuf message type must have a converter
    elif isinstance(_value, _Message) and not isinstance(_value, MessageWrapper):
        raise NotImplementedError(
            f"converter for protobuf type {_field_type} is not implemented"
        )
    # for type without converter defined and not protobuf message type
    return _value


# converter base


class _ProtobufConverter(Generic[_MessageType]):
    """Base class for all message converter class."""

    proto_class: Type[_MessageType]

    @classmethod
    @abstractmethod
    def convert(cls, _in: _MessageType) -> Self:
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
    def register(
        cls,
        message_type: Type[_MessageType],
        converter_type: Type[_ProtobufConverter[_MessageType]],
    ):
        if not message_type in cls.TYPE_CONVERTER_REGISTER:
            cls.TYPE_CONVERTER_REGISTER[message_type] = converter_type

    @classmethod
    def get_converter(
        cls, message_type: Type[_MessageType], default=None
    ) -> Optional[_ProtobufConverter[_MessageType]]:
        return cls.TYPE_CONVERTER_REGISTER.get(message_type, default)


# well-known type converter
# check https://protobuf.dev/reference/python/python-generated/#wkt for details

# NOTE: only support Duration as we only use Duration currently.
# NOTE: concrete wrapper implementation should register the DurationWrapper
#       whenever Duration protobuf type is used.


_pb2_duration = cast(Type[_Duration], object)


class Duration(_ProtobufConverter[_Duration], int, _pb2_duration):  # type: ignore
    proto_class = _Duration

    @classmethod
    def from_ns(cls, value: int) -> Self:
        """Helper method to convert an int to this class' inst.

        NOTE: although we can directly use DurationWrapper(<value>), the
              static type checker will complain, so we use this helper
              method instead.
        """
        return cls(value)  # type: ignore

    @classmethod
    def convert(cls, _in: _Duration) -> Self:
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


class ListLikeContainerWrapper(_ProtobufConverter[List[Any]], list):  # type: ignore
    proto_class = list  # NOTE: just a placeholder, don't use

    def __str__(self) -> str:
        _buffer = io.StringIO()
        _buffer.write("[\n")
        for _entry in self:
            for _line in str(_entry).splitlines(keepends=True):
                _buffer.write(f"\t{_line}")
            _buffer.write(",\t\n")
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


# all list in wrapper should be converted to ListLikeContainerWrapper
TypeConverterRegister.register(list, ListLikeContainerWrapper)


# message converter


class MessageWrapperBase(Generic[_MessageType]):
    """Base class for all message wrapper type."""

    proto_class: Type[_MessageType]
    __slots__ = []

    def __init__(self, /, **kwargs):
        """Get all attrs from a protobuf class inst and then bind to this inst.
        All the input kwargs are expected to be converted by __new__ method from
        MessageWrapper.

        __init__ method is defined here to be hidden by typing.cast to not override
        the wrapped protoclass' __init__ signature when type checking.

        Only kwargs are allowed.
        """
        for _key in self.__slots__:
            setattr(self, _key, kwargs.get(_key))


class MessageWrapper(_ProtobufConverter[_MessageType]):
    proto_class: Type[_MessageType]
    __slots__ = []

    # internal

    @overload
    def __new__(cls, _in: _MessageType, /) -> Self:
        ...

    @overload
    def __new__(cls, /, **kwargs) -> Self:
        ...

    def __new__(cls, _in=None, **kwargs) -> Self:
        parsed_kwargs = {}
        if _in:  # initialize by converting an incoming protobuf message
            if not isinstance(_in, cls.proto_class):
                raise TypeError(
                    f"wrong input type, expect={cls.proto_class}, in={_in.__class__}"
                )
            # only capture the attrs presented in this wrapper class' __slots__
            for _attrn in cls.__slots__:
                _attrv = getattr(_in, _attrn)
                parsed_kwargs[_attrn] = _general_converter(_attrv)
        else:  # initialize by manually create wrapper instance
            # Create a wrapper instance directly with optional input attrs.
            # NOTE: recursive message is NOT supported!
            _template = cls.proto_class()
            for _attrn in cls.__slots__:
                # assign from default value if attr for attrn is not specified
                if (_attrv := kwargs.get(_attrn, None)) is None:
                    _attrv = getattr(_template, _attrn)
                parsed_kwargs[_attrn] = _general_converter(_attrv)
        return cls(**parsed_kwargs)

    def __getitem__(self, __name: str) -> Any:
        return getattr(self, __name)

    def __setitem__(self, __name: str, __value: Any):
        return setattr(self, __name, _general_converter(__value))

    def __eq__(self, __o: object) -> bool:
        if self.__class__ != __o.__class__:
            return False
        for _attrn in self.__slots__:
            if getattr(self, _attrn) != getattr(__o, _attrn):
                return False
        return True

    def __str__(self) -> str:
        _buffer = io.StringIO()
        _buffer.write("{\n")
        for _attrn in self.__slots__:
            _attrv_str = str(getattr(self, _attrn))
            _buffer.write(f" {_attrn} :")
            for _idx, _line in enumerate(_attrv_str.splitlines(keepends=True)):
                if _idx == 0:
                    _buffer.write(f"{_line}")
                else:
                    _buffer.write(f"\t {_line}")
            _buffer.write(",\n")
        _buffer.write("}")
        return _buffer.getvalue()

    # public API

    @classmethod
    def convert(cls, _in: _MessageType) -> Self:
        """Copy and wrap input message into a new wrapper instance."""
        return cls(_in)

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

# NOTE: as for protoc==3.21.11, protobuf==4.21.12, at runtime the
#       type of protobuf Enum value is int, the enum value itself
#       is not the instance of any Enum type defined in protobuf
#       package. So no extra enum wrapper type is defined here,
#       all enum is used and assigned as int.
