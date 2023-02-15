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
from abc import abstractmethod, ABC
from copy import deepcopy
from io import StringIO
from google.protobuf.message import Message as _Message
from google.protobuf.duration_pb2 import Duration as _Duration
from typing import (
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


_T = TypeVar("_T")  # any generic types
_MessageType = TypeVar("_MessageType", bound=_Message)
_NormalType = TypeVar("_NormalType")  # other types other than protobuf message type

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


_WrappedMessageType = TypeVar("_WrappedMessageType", bound=_ProtobufConverter)

# helper method


@overload
def _convert_helper(
    _value: _MessageType, _field_type: Optional[Type[_MessageType]] = None
) -> _WrappedMessageType:  # type: ignore
    ...


@overload
def _convert_helper(
    _value: _NormalType, _field_type: Optional[Type[_NormalType]] = None
) -> _NormalType:
    ...


def _convert_helper(_value, _field_type=None):
    """For message types that have corresponding converter,
    convert the message to wrapped version using the converter."""
    if not _field_type:
        _field_type = type(_value)

    # if converter for this type is available, always use it
    if _converter := TypeConverterRegister.get_converter(_field_type):
        return _converter.convert(_value)
    # protobuf message type must have a converter
    elif isinstance(_value, _Message) and not isinstance(_value, MessageWrapper):
        raise NotImplementedError(
            f"converter for protobuf type {_field_type} is not implemented"
        )
    else:
        # for type without converter defined and not protobuf message type
        return _value


# the converter mapping between protobuf message/container type and converter type


class TypeConverterRegister:
    TYPE_CONVERTER_REGISTER = {}

    def __new__(cls, *args, **kwargs) -> None:
        raise ValueError("this class should not be instantiated")

    @classmethod
    def register(cls, message_type, converter_type) -> None:
        if not message_type in cls.TYPE_CONVERTER_REGISTER:
            cls.TYPE_CONVERTER_REGISTER[message_type] = converter_type

    @classmethod
    def get_converter(
        cls, message_type, default=None
    ) -> Optional[Type[_ProtobufConverter[Any]]]:
        return cls.TYPE_CONVERTER_REGISTER.get(message_type, default)


# well-known type converter
# check https://protobuf.dev/reference/python/python-generated/#wkt for details

# NOTE: only support Duration as we only use Duration currently.
# NOTE: concrete wrapper implementation should register the DurationWrapper
#       whenever Duration protobuf type is used.


class DurationWrapper(_ProtobufConverter[_Duration], int):
    """Convert and store protobuf Duration counted by nanoseconds as int."""

    proto_class = _Duration

    @classmethod
    def convert(cls, _in: _Duration) -> Self:
        return cls(_in.ToNanoseconds())

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


class _ContainerBase(List[_T], ABC):
    def __str__(self) -> str:
        _buffer = StringIO()
        _buffer.write("[\n")
        for _entry in self:
            for _line in str(_entry).splitlines(keepends=True):
                _buffer.write(f"\t{_line}")
            _buffer.write(",\t\n")
        _buffer.write("]")
        return _buffer.getvalue()


class RepeatedCompositeContainer(
    _ContainerBase[_WrappedMessageType],
    _ProtobufConverter[List[_MessageType]],  # type: ignore
):
    proto_class = list  # placeholder, not use!

    @classmethod
    def convert(cls, _in: List[_MessageType]) -> Self:
        res = cls()
        for _entry in _in:
            res.append(_convert_helper(_entry))
        return res

    def export_pb(self) -> List[_MessageType]:
        return [_entry.export_pb() for _entry in self]


class RepeatedScalarContainer(
    _ContainerBase[_NormalType],
    _ProtobufConverter[List[_NormalType]],  # type: ignore
):
    proto_class = list  # placeholder, not use!

    @classmethod
    def convert(cls, _in: List[_NormalType]) -> List[_NormalType]:
        return cls(_in)

    def export_pb(self) -> List[_NormalType]:
        return deepcopy(self)


# message converter


class MessageWrapper(_ProtobufConverter[_MessageType]):
    proto_class: Type[_MessageType]
    __slots__: List[str] = []

    # internal
    @overload
    def __new__(cls, _in: _MessageType, /) -> Self:
        """Initialize by converting a protobuf message."""

    @overload
    def __new__(cls, _=None, /, **kwargs) -> Self:
        """Initialize by manually creating wrapper instance."""

    def __new__(cls, _in=None, /, **kwargs) -> Self:
        parsed_kwargs = {}
        if _in:  # initialize by converting an incoming protobuf message
            if not isinstance(_in, cls.proto_class):
                raise TypeError(
                    f"wrong input type, expect={cls.proto_class}, in={_in.__class__}"
                )
            # only capture the attrs presented in this wrapper class' __slots__
            for _attrn in cls.__slots__:
                _attrv = getattr(_in, _attrn)
                parsed_kwargs[_attrn] = _convert_helper(_attrv)
        else:  # initialize by manually create wrapper instance
            # Create a wrapper instance directly with optional input attrs.
            # NOTE: recursive message is NOT supported!
            _template = cls.proto_class()
            for _attrn in cls.__slots__:
                # assign from default value if attr for attrn is not specified
                if (_attrv := kwargs.get(_attrn, None)) is None:
                    _attrv = getattr(_template, _attrn)
                if isinstance(_attrv, list):
                    # convert list to either CompositeContainer or ScalarContainer
                    _attrv = _convert_helper(_attrv, type(getattr(_template, _attrn)))
                parsed_kwargs[_attrn] = _convert_helper(_attrv)

        (_inst := super().__new__(cls))._post_init(**parsed_kwargs)
        return _inst

    def _post_init(self, **kwargs):
        """Get all attrs from a protobuf class inst and then bind to this inst.
        All the input kwargs are converted by __new__ method from
        MessageWrapper.

        Note that normal __init__ is not used to avoid override the __init__
        signature from wrapped protobuf message.
        """
        for _key in self.__slots__:
            setattr(self, _key, kwargs.get(_key))

    def __getitem__(self, __name: str) -> Any:
        return getattr(self, __name)

    def __setitem__(self, __name: str, __value: Any):
        return setattr(self, __name, _convert_helper(__value))

    def __eq__(self, __o: object) -> bool:
        if self.__class__ != __o.__class__:
            return False
        for _attrn in self.__slots__:
            if getattr(self, _attrn) != getattr(__o, _attrn):
                return False
        return True

    def __str__(self) -> str:
        _buffer = StringIO()
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
                elif isinstance(_value, _ContainerBase):
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
