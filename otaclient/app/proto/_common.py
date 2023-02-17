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
from enum import IntEnum, EnumMeta
from io import StringIO
from google.protobuf.duration_pb2 import Duration as _Duration
from typing import (
    Optional,
    Tuple,
    overload,
    get_type_hints,
    get_origin,
    get_args,
    Any,
    Dict,
    List,
    MutableMapping,
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


class ProtobufConverter(Generic[_MessageType], ABC):
    """Base class for all message converter class."""

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
    if isinstance(tp, type):
        return tp
    # if field is generic typed
    return _origin if (_origin := get_origin(tp)) else tp


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

    # NOTE: don't use isinstance() to check wether _value is scalar value type
    #       as protobuf enum value is pure int at runtime.
    if _annotated_type in _NORMAL_PYTHON_TYPES and type(_value) is _annotated_type:
        return _value  # scalar field

    _field_type = _reveal_origin_type(_annotated_type)
    if issubclass(_field_type, (MessageWrapper, EnumWrapper)):
        return _field_type.convert(_value)
    if issubclass(
        _field_type,
        (
            RepeatedCompositeContainer,
            RepeatedScalarContainer,
            MessageMapping,
        ),
    ):
        return _field_type.convert(_value, _annotation=_annotated_type)
    raise TypeError(f"failed to convert {_value}({type(_value)}) to {_annotated_type=}")


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


class _ListLikeContainerBase(List[_T]):
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
    ProtobufConverter[List[_MessageType]],
    _ListLikeContainerBase[_WrappedMessageType],
    Generic[_WrappedMessageType, _MessageType],
):
    @staticmethod
    def _get_annotated_types(
        _annotation,
    ) -> Tuple[Type[_WrappedMessageType], Type[_MessageType]]:
        """Get real types from annotation.

        Properly annotation for RepeatedCompositeContainer is as follow:
        RepeatedCompositeContainer[WrappedMessageType, MessageType]

        Call get_args on above annotation will return a tuple of
        WrappedMessageType and MessageType.
        """
        if len(_types_tuple := get_args(_annotation)) != 2 and not issubclass(
            _types_tuple[0], ProtobufConverter
        ):
            raise TypeError(f"badly annotated repeated field: {_annotation=}")

        return _types_tuple

    @classmethod
    def convert(
        cls,
        _in: Union[Iterable[_MessageType], Iterable[_WrappedMessageType]],
        /,
        _annotation: Any,
    ) -> Self:
        _wrapper_type, _messge_type = cls._get_annotated_types(_annotation)

        res = cls()
        for _entry in _in:
            # type check and skip already converted elements,
            # ensure the elements in res all in same type and converted.
            if isinstance(_entry, _wrapper_type):
                res.append(_entry)
            elif isinstance(_entry, _messge_type):
                res.append(_wrapper_type.convert(_entry))
            else:
                raise TypeError(
                    f"all elements in the container should have the same type,"
                    f"expecting {_wrapper_type=} or {_messge_type}, get {type(_entry)=}"
                )
        return res

    def export_pb(self) -> List[_MessageType]:
        return [_entry.export_pb() for _entry in self]


class RepeatedScalarContainer(
    ProtobufConverter[List[_NormalType]],
    _ListLikeContainerBase[_NormalType],
    Generic[_NormalType],
):
    @staticmethod
    def _get_annotated_types(_annotation) -> Type[_NormalType]:
        """Get real types from annotation.

        Properly annotation for RepeatedScalarContainer is as follow:
        RepeatedScalarContainer[NormalType]

        Call get_args on above annotation will return NormalType.
        """
        if (
            len(_types_tuple := get_args(_annotation)) != 1
            and not _types_tuple[0] in _NORMAL_PYTHON_TYPES
        ):
            raise TypeError(f"badly annotated repeated field: {_annotation=}")

        return _types_tuple[0]

    @classmethod
    def convert(cls, _in: Iterable[_NormalType], /, _annotation) -> Self:
        # conduct type checks over all elements
        res = cls()

        _element_type = cls._get_annotated_types(_annotation)
        for _entry in _in:
            # NOTE: strict type check is applied for scalar field
            if type(_entry) is not _element_type:
                raise TypeError(f"expect {_element_type=}, get {type(_entry)=}")
            res.append(_entry)
        return res

    def export_pb(self) -> List[_NormalType]:
        return self.copy()


_K, _Converted_V = TypeVar("_K", int, str, bool), TypeVar("_Converted_V")


class MessageMapping(
    # key_type in maps can be any scalar types except float and bytes
    # value_type can be anything except another mapping
    ProtobufConverter[MutableMapping[_K, _T]],
    Dict[_K, _Converted_V],
    Generic[_K, _Converted_V, _T],
):
    @staticmethod
    def _get_annotated_types(
        _annotation,
    ) -> Tuple[Type[_K], Type[_Converted_V], Type[_T]]:
        """Get real types from annotation.

        Properly annotation for Mapping is as follow:
        MessageMapping[_K, _Converted_V, <protobuf_mapping_value_type: T>]
        """
        if len(_types_tuple := get_args(_annotation)) != 3:
            raise TypeError(f"badly annotated repeated field: {_annotation=}")
        return _types_tuple

    @classmethod
    def convert(cls, _in: MutableMapping[_K, Any], /, _annotation) -> Self:
        _ktp, _vtp, _ = cls._get_annotated_types(_annotation)

        res = cls()
        for _k, _v in _in.items():
            if type(_k) is not _ktp:
                raise TypeError(f"expect key type={_ktp}, get {type(_k)=}")
            res[_k] = _convert_helper(_v, _vtp)
        return res

    def export_pb(self) -> Dict[_K, _T]:
        res = {}
        for _k, _converted_v in self.items():
            if type(_converted_v) in _NORMAL_PYTHON_TYPES:
                res[_k] = _converted_v
            elif isinstance(_converted_v, ProtobufConverter):
                res[_k] = _converted_v.export_pb()
            else:
                raise ValueError(
                    f"failed to export {_converted_v=}({type(_converted_v)=})"
                )
        return res


# enum converter

# NOTE: as for protoc==3.21.11, protobuf==4.21.12, at runtime the
#       type of protobuf Enum value is int, the enum value itself
#       is not the instance of any Enum type defined in generated
#       protobuf types.


class _DefaultValueEnumMeta(EnumMeta):
    """Align the protobuf enum behavior that the default value is
    the first enum in defined order(typically 0 at runtime)."""

    def __call__(cls, *args, **kwargs):
        if not args and not kwargs:
            return next(iter(cls))  # type: ignore
        return super().__call__(*args, **kwargs)


class EnumWrapper(IntEnum, metaclass=_DefaultValueEnumMeta):
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


# NOTE: EnumWrapper cannot directly inherit from _ProtobufConverter,
#       so we virtually inherit by registering to _ProtobufConverter
ProtobufConverter.register(EnumWrapper)


# message converter


class MessageWrapper(ProtobufConverter[_MessageType]):
    proto_class: Type[_MessageType]
    _field_types: Dict[str, Any]
    # if field is typed as generic, get the actual original type
    _field_types_origin: Dict[str, type]
    __slots__: List[str]

    # internal

    def __init_subclass__(cls) -> None:
        """Parse type annotations defined in wrapper class."""
        cls._field_types, cls._field_types_origin = {}, {}
        for _field_name, _field_type in get_type_hints(cls).items():
            if not (_field_name.startswith("_") or _field_name == "proto_class"):
                cls._field_types[_field_name] = _field_type
            # for generic typed field, like repeated field
            if _origin := get_origin(_field_type):
                cls._field_types_origin[_field_name] = _origin

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
                    cls._field_types[_attrn],
                )
        else:  # initialize by manually create wrapper instance
            for _attrn in cls.__slots__:
                if not _attrn in kwargs:
                    # let the constructor of each field type do the job
                    parsed_kwargs[_attrn] = cls._get_real_type(_attrn)()
                else:
                    # NOTE: pass the original annotation to the converter
                    #       generic typed field needs this information
                    parsed_kwargs[_attrn] = _convert_helper(
                        kwargs[_attrn],
                        cls._field_types[_attrn],
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
        setattr(self, __name, __value)

    def __setattr__(self, __name: str, __value: Any) -> None:
        if __name not in self.__slots__:
            raise AttributeError(f"field {__name} is not defined")
        # type check the assigned value
        if not isinstance(__value, _field_type := self._get_real_type(__name)):
            raise TypeError(f"expect {_field_type=}, get {type(__value)=}")
        return super().__setattr__(__name, __value)

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

    # helper

    @classmethod
    def _get_real_type(cls, _field_name: str) -> type:
        """Get real type for field(resolve generic typed field)."""
        if _tp := cls._field_types_origin.get(_field_name):
            return _tp
        return cls._field_types.get(_field_name)  # type: ignore

    # public API

    @classmethod
    def convert(cls, _in: _MessageType) -> Self:
        """Copy and wrap input message into a new wrapper instance."""
        return cls(_in)  # actual converting is handled by __new__

    def export_pb(self) -> _MessageType:
        """Export as protobuf message class inst."""
        _res = self.proto_class()
        for _field in self.__slots__:
            try:
                _value = getattr(self, _field)
            except AttributeError:
                continue

            # apply strict checking over scalar value field
            if type(_value) in _NORMAL_PYTHON_TYPES:
                setattr(_res, _field, _value)
            elif isinstance(_value, EnumWrapper):
                setattr(_res, _field, _value.export_pb())
            # normal message and well-known types
            elif isinstance(_value, (MessageWrapper, Duration)):
                getattr(_res, _field).CopyFrom(_value.export_pb())
            # repeated field
            elif isinstance(
                _value, (RepeatedCompositeContainer, RepeatedScalarContainer)
            ):
                getattr(_res, _field).extend(_value.export_pb())
            else:
                raise ValueError(f"failed to export {_value=} to {self.proto_class=}")
        return _res


# well-known type converter
# check https://protobuf.dev/reference/python/python-generated/#wkt for details

# NOTE: only support Duration as we only use Duration currently.
# NOTE: concrete wrapper implementation should register the DurationWrapper
#       whenever Duration protobuf type is used.


class Duration(MessageWrapper[_Duration]):
    """Wrapper for protobuf well-known type Duration.

    NOTE: this wrapper supports directly adding nanoseconds.
    """

    proto_class = _Duration
    __slots__ = ["seconds", "nanos"]
    seconds: int
    nanos: int
    _ns2s = 1_000_000_000

    def __init__(
        self, seconds: Optional[int] = ..., nanos: Optional[int] = ...
    ) -> None:
        ...

    @classmethod
    def from_nanoseconds(cls, _ns: int) -> Self:
        seconds, nanos = divmod(_ns, cls._ns2s)
        return cls(seconds=seconds, nanos=nanos)

    def add_nanoseconds(self, _ns: int):
        seconds, nanos = divmod(_ns, self._ns2s)
        self.seconds += seconds
        self.nanos += nanos
