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
from enum import IntEnum
from io import StringIO
from google.protobuf.message import Message as _Message
from google.protobuf.duration_pb2 import Duration as _Duration
from typing import (
    overload,
    get_type_hints,
    get_origin,
    get_args,
    Any,
    Dict,
    List,
    Iterable,
    Generic,
    Type,
    TypeVar,
    Union,
)
from typing_extensions import Self


_T = TypeVar("_T")
_MessageType = TypeVar("_MessageType")  # expected to cover all protobuf types, and list
# built-in python types that directly being used in protobuf message
_NormalType = TypeVar("_NormalType", float, int, str, bytes, bool)
_NORMAL_PYTHON_TYPES = (float, int, str, bytes, bool)


# converter base


def parse_type_hint(cls: Type[MessageWrapper]) -> Type[MessageWrapper]:
    """Parse type annotations defined in wrapper class."""
    if not issubclass(cls, MessageWrapper):
        raise TypeError(
            "this decorator should only be used on MessageWrapper derived classes"
        )
    parsed = {}
    for _k, _v in get_type_hints(cls).items():
        if _k.startswith("_") or _k == "proto_class":
            continue
        parsed[_k] = _v

    cls._type_hints = parsed
    cls._field_types = {
        _n: _reveal_origin_type(_t) for _n, _t in cls._type_hints.items()
    }
    return cls


class ProtobufConverter(Generic[_MessageType], ABC):
    """Base class for all message converter class."""

    proto_class: Type[_MessageType]

    @classmethod
    @abstractmethod
    def convert(cls, _in: _MessageType, /, **kwargs) -> Self:
        raise NotImplementedError

    @abstractmethod
    def export_pb(self) -> _MessageType:
        raise NotImplementedError


_WrappedMessageType = TypeVar("_WrappedMessageType", bound=ProtobufConverter)


# helper method


def _reveal_origin_type(tp: Type[_T]) -> Type[_T]:
    return _revealed_tp if (_revealed_tp := get_origin(tp)) else tp


def _convert_helper(
    _value: Any,
    _annotated_type: Union[Type[_NormalType], Type[_WrappedMessageType]],
) -> Union[_NormalType, _WrappedMessageType]:
    """Take any input, and convert it to wrapped version if needed.

    The convertion rules are as follow:
    - values in normal built-in python types will be return as it,
    - already converted messages will be return as it,
    - protobuf message will be converted to corresponding wrapper's instance,
    - iterable value will be consumed and converted into container wrapper types,
    """
    # directly return on already converted
    if isinstance(_value, ProtobufConverter):
        return _value  # type: ignore
    # NOTE: don't use isinstance() to check wether _value is normal type
    #       as protobuf enum and Duration both inherits from int
    if type(_value) in _NORMAL_PYTHON_TYPES:
        return _value

    # parsing complicated things...
    # get the real field type from annotation
    if (_field_type := get_origin(_annotated_type)) is None:
        _field_type = _annotated_type

    if isinstance(_value, Iterable):
        if issubclass(_field_type, RepeatedScalarContainer):
            return _field_type.convert(_value)
        elif issubclass(_field_type, RepeatedCompositeContainer):
            return _field_type.convert(_value, annotation=_annotated_type)
        else:
            raise TypeError(
                f"failed to convert list like field {_value=}, {_annotated_type=}"
            )
    # if converter is available, always use it
    elif issubclass(_field_type, ProtobufConverter):
        return _field_type.convert(_value)

    raise TypeError(f"failed to convert {_value}({type(_value)}) to {_annotated_type=}")


# well-known type converter
# check https://protobuf.dev/reference/python/python-generated/#wkt for details

# NOTE: only support Duration as we only use Duration currently.
# NOTE: concrete wrapper implementation should register the DurationWrapper
#       whenever Duration protobuf type is used.


class DurationWrapper(ProtobufConverter[_Duration], int):
    """Convert and store protobuf Duration counted by nanoseconds as int.

    NOTE: this wrapper inherits from int, so be very careful when distinguish
          it with normal python type int(isinstance should not be used!).
    """

    proto_class = _Duration

    @classmethod
    def convert(cls, _in: Union[_Duration, Self]) -> Self:
        if isinstance(_in, _Duration):
            return cls(_in.ToNanoseconds())
        return _in

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


class _ContainerBase(List[_T]):
    def __str__(self) -> str:
        _buffer = StringIO()
        _buffer.write("[\n")
        for _entry in self:
            for _line in str(_entry).splitlines(keepends=True):
                _buffer.write(f"\t{_line}")
            _buffer.write(",\t\n")
        _buffer.write("]")
        return _buffer.getvalue()

    __repr__ = __str__


class RepeatedCompositeContainer(
    _ContainerBase[_WrappedMessageType], ProtobufConverter[List[_MessageType]]
):
    proto_class = _ContainerBase

    @classmethod
    def convert(
        cls,
        _in: Union[Iterable[_MessageType], Iterable[_WrappedMessageType]],
        /,
        annotation: Any,
    ) -> Self:
        # NOTE: a proper type hinted repeated field in wrapper definition should have
        #       get_args(annotation) = (<element_wrapper_type>, <protobuf_message_type>)
        if len(_types_tuple := get_args(annotation)) != 2:
            raise TypeError("repeated field is not properly type annotated")
        if not issubclass(_wrapper_type := _types_tuple[0], ProtobufConverter):
            raise TypeError("badly annotated repeated field")

        res = cls()
        for _entry in _in:
            # type check and skip already converted elements
            if isinstance(_entry, ProtobufConverter):
                if not isinstance(_entry, _wrapper_type):
                    raise TypeError(f"expect {_wrapper_type}, but get {type(_entry)=}")
                res.append(_entry)  # type: ignore
            else:
                res.append(_wrapper_type.convert(_entry))  # type: ignore
        return res

    def export_pb(self) -> List[_MessageType]:
        return [_entry.export_pb() for _entry in self]


class RepeatedScalarContainer(
    _ContainerBase[_NormalType], ProtobufConverter[List[_NormalType]]
):
    proto_class = _ContainerBase

    @classmethod
    def convert(cls, _in: Iterable[_NormalType]) -> Self:
        # conduct type checks over all elements
        res = cls()
        for _entry in _in:
            if type(_entry) not in _NORMAL_PYTHON_TYPES:
                raise TypeError
            res.append(_entry)
        return res

    def export_pb(self) -> List[_NormalType]:
        return self.copy()


# enum converter

# NOTE: as for protoc==3.21.11, protobuf==4.21.12, at runtime the
#       type of protobuf Enum value is int, the enum value itself
#       is not the instance of any Enum type defined in generated
#       protobuf types.


class EnumWrapper(IntEnum):
    @classmethod
    def get_default_value(cls) -> Self:
        """Align to protobuf, the default value for Enum type will be 0."""
        return cls(0)

    @classmethod
    def convert(cls, _in: Union[int, str, Self]) -> Self:
        if isinstance(_in, int):
            return cls(_in)
        elif isinstance(_in, str):
            return cls[_in]
        elif isinstance(_in, cls):
            return _in
        else:
            raise TypeError(f"cannot convert {_in} into {cls}")

    def export_pb(self) -> int:
        return self.value


# align EnumWrapper to ProtobufConverter spec
setattr(EnumWrapper, "proto_class", EnumWrapper)
# NOTE: EnumWrapper cannot directly inherit from _ProtobufConverter,
#       so we virtually inherit by registering to _ProtobufConverter
ProtobufConverter.register(EnumWrapper)


# message converter


class MessageWrapper(ProtobufConverter[_MessageType]):
    proto_class: Type[_MessageType]
    _type_hints: Dict[str, Any]
    _field_types: Dict[str, Any]
    __slots__: List[str]

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
            if isinstance(_in, cls):
                return _in
            if not isinstance(_in, cls.proto_class):
                raise TypeError(f"expect={cls.proto_class}, in={_in.__class__}")

            # only capture the attrs presented in this wrapper class' __slots__
            for _attrn in cls.__slots__:
                parsed_kwargs[_attrn] = _convert_helper(
                    getattr(_in, _attrn),
                    cls._type_hints[_attrn],
                )
        else:  # initialize by manually create wrapper instance
            for _attrn in cls.__slots__:
                if not _attrn in kwargs:
                    # let the constructor do the job
                    if issubclass(_field_type := cls._field_types[_attrn], EnumWrapper):
                        parsed_kwargs[_attrn] = _field_type.get_default_value()
                    else:
                        parsed_kwargs[_attrn] = cls._field_types[_attrn]()
                else:
                    parsed_kwargs[_attrn] = _convert_helper(
                        kwargs[_attrn],
                        cls._type_hints[_attrn],
                    )

        (_inst := super().__new__(cls))._post_init(**parsed_kwargs)
        return _inst

    def _post_init(self, **kwargs):
        """Get all attrs from a protobuf class inst and then bind to this inst.
        All the input kwargs are converted by __new__ method from
        MessageWrapper.

        Note that normal __init__ is not used to allow type hintings on __init__
            without assigning all attributes again.
        """
        for _key in self.__slots__:
            setattr(self, _key, kwargs.get(_key))

    def __getitem__(self, __name: str) -> Any:
        return getattr(self, __name)

    def __setitem__(self, __name: str, __value: Any):
        if not isinstance(__value, ProtobufConverter):
            raise TypeError("should not assign original protobuf message to wrapper")
        return setattr(self, __name, __value)

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

    __repr__ = __str__

    # public API

    @classmethod
    def convert(cls, _in: _MessageType) -> Self:
        """Copy and wrap input message into a new wrapper instance."""
        return cls(_in)  # actual converting is handled by __new__

    def export_pb(self) -> _MessageType:
        """Export as protobuf message class inst."""
        _res = self.proto_class()
        for _key in self.__slots__:
            try:
                _value = getattr(self, _key)
            except AttributeError:
                continue

            # if _value is the converted version, export to pb first
            if isinstance(_value, ProtobufConverter):
                # for protobuf message, we should use <CopyFrom> API to assign the value
                if issubclass(_value.proto_class, _Message):
                    getattr(_res, _key).CopyFrom(_value.export_pb())
                # for value in EnumWrapper type, directly assign exported value
                elif issubclass(_value.proto_class, EnumWrapper):
                    setattr(_res, _key, _value.export_pb())
                # for repeated field, use <extend> API to assign
                elif isinstance(_value, _ContainerBase):
                    getattr(_res, _key).extend(_value.export_pb())
                else:
                    raise ValueError
            # for attr in normal python type, directly assign
            elif type(_value) in _NORMAL_PYTHON_TYPES:
                setattr(_res, _key, _value)
            else:
                raise ValueError(f"failed to export {_value=} to {self.proto_class=}")
        return _res
