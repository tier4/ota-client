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

from abc import ABC, abstractmethod
from copy import deepcopy
from enum import EnumMeta, IntEnum
from functools import update_wrapper
from io import StringIO
from typing import (
    Any,
    Dict,
    Generic,
    Iterable,
    List,
    Mapping,
    Optional,
    Type,
    TypeVar,
    Union,
    get_args,
    get_origin,
    get_type_hints,
    overload,
)

from google.protobuf.duration_pb2 import Duration as _Duration
from google.protobuf.message import Message as _pb_Message
from typing_extensions import Self, deprecated

__all__ = [
    # ------ type vars ------ #
    "MessageType",
    "ScalarValueType",
    "MessageWrapperType",
    "FieldContainerWrapperType",
    "SCALAR_VALUE_TYPES",
    # ------ detailed core types/utils ------ #
    "calculate_slots",
    # wrapper base
    "MessageWrapper",
    "EnumWrapper",
    # well-known types
    "Duration",
    # container field wrapper
    "RepeatedCompositeContainer",
    "RepeatedScalarContainer",
    "ScalarMapContainer",
    "MessageMapContainer",
]

# typing helpers

_T = TypeVar("_T")
MessageType = TypeVar("MessageType", bound=_pb_Message)
# built-in python types that directly being used in protobuf message
ScalarValueType = TypeVar("ScalarValueType", float, int, str, bytes, bool)
SCALAR_VALUE_TYPES = (float, int, str, bytes, bool)

MessageWrapperType = TypeVar("MessageWrapperType", bound="MessageWrapper")
EnumWrapperType = TypeVar("EnumWrapperType", bound="EnumWrapper")
FieldContainerWrapperType = TypeVar("FieldContainerWrapperType", bound="_ContainerBase")

# helper method


def _reveal_origin_type(tp: Type[_T]) -> Type[_T]:
    """Return the actual type from generic alias,
    or return as it if input type is not generic alias."""
    if _origin := get_origin(tp):
        return _origin
    elif isinstance(tp, type):
        return tp
    raise TypeError(f"{tp=} is not a valid type/type annotation")


def calculate_slots(_proto_msg_type: Type[_pb_Message]) -> List[str]:
    """Calculate the __slots__ for input proto message type.

    Since we are using field descriptors in wrapper creating, attribute values
        are not stored in the actual field name. This function creates the slots
        with the actual attribute name for each field.
    """
    _field_names = list(_proto_msg_type.DESCRIPTOR.fields_by_name)
    return [_get_field_attrn(_fn) for _fn in _field_names]


# base


class WrapperBase(Generic[_T], ABC):
    """Base for all wrapper types."""

    @classmethod
    @abstractmethod
    def convert(cls, _in: _T, /, **kwargs):
        """Convert"""

    @abstractmethod
    def export_pb(self) -> _T:
        """Export itself to protobuf types, or containers that hold
        protobuf types."""

    @abstractmethod
    def export_to_pb(self, pb_msg: _pb_Message, field_name: str):
        """Export itself to <field_name> in <pb_msg>"""


# container converter


class _ContainerBase(WrapperBase):
    """
    NOTE: the container wrapper types are not meant to be instantiated
    manually, please use convert API to convert a list/dict containing messages.
    """


class _ListLikeContainerBase(List[_T], _ContainerBase):
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


class RepeatedCompositeContainer(_ListLikeContainerBase[MessageWrapperType]):
    def __init__(self, *, converter_type: Type[MessageWrapperType]) -> None:
        self.converter_type = converter_type
        self.message_type = converter_type._proto_class

    @classmethod
    def convert(
        cls, _in: Iterable[Any], /, converter_type: Type[MessageWrapperType]
    ) -> Self:
        res = cls(converter_type=converter_type)
        _proto_msg_type = converter_type._proto_class
        for _entry in _in:
            if isinstance(_entry, converter_type):
                super(_ListLikeContainerBase, res).append(_entry)
            elif isinstance(_entry, _proto_msg_type):
                super(_ListLikeContainerBase, res).append(
                    converter_type.convert(_entry)
                )
            else:
                raise TypeError(
                    f"all elements in the container should have the same type,"
                    f"expecting {converter_type} or {_proto_msg_type}, get {type(_entry)=}"
                )
        return res

    def export_pb(self) -> List[Any]:
        return [_entry.export_pb() for _entry in self]

    def export_to_pb(self, pb_msg: _pb_Message, field_name: str):
        _pb_container: List[Any] = getattr(pb_msg, field_name)
        for _entry in self:
            _pb_container.append(_entry.export_pb())

    # type checked API method

    def append(self, __object: Any) -> None:
        if isinstance(__object, self.converter_type):
            return super().append(__object)
        if isinstance(__object, self.message_type):
            return super().append(self.converter_type.convert(__object))
        raise TypeError

    def extend(self, __iterable: Iterable[Any]) -> None:
        for _element in __iterable:
            self.append(_element)


class RepeatedScalarContainer(_ListLikeContainerBase[ScalarValueType]):
    def __init__(self, *, element_type: Type[ScalarValueType]) -> None:
        self.element_type = element_type

    @classmethod
    def convert(
        cls, _in: Iterable[ScalarValueType], /, element_type: Type[ScalarValueType]
    ) -> Self:
        res = cls(element_type=element_type)
        for _entry in _in:
            # NOTE: strict type check is applied for scalar field
            if type(_entry) is not element_type:
                raise TypeError(f"expect {element_type=}, get {type(_entry)=}")
            res.append(_entry)
        return res

    def export_pb(self) -> List[ScalarValueType]:
        return self.copy()

    def export_to_pb(self, pb_msg: _pb_Message, field_name: str):
        _pb_container: List[Any] = getattr(pb_msg, field_name)
        _pb_container.extend(self)

    # type checked API method

    def append(self, __object: Any) -> None:
        if isinstance(__object, self.element_type):
            return super().append(__object)
        raise TypeError

    def extend(self, __iterable: Iterable[Any]) -> None:
        for _element in __iterable:
            self.append(_element)


_K = TypeVar("_K", int, str, bool)


class _MappingLikeContainerBase(Dict[_K, _T], _ContainerBase): ...


class MessageMapContainer(_MappingLikeContainerBase[_K, MessageWrapperType]):
    def __init__(
        self,
        *,
        key_type: Type[_K],
        value_converter: Type[MessageWrapperType],
    ) -> None:
        self.key_type = key_type
        self.value_converter = value_converter
        self.value_type = value_converter._proto_class

    @classmethod
    def convert(
        cls,
        _in: Mapping[_K, Any],
        /,
        key_type: Type[_K],
        value_converter: Type[MessageWrapperType],
    ) -> Self:
        res = cls(key_type=key_type, value_converter=value_converter)
        _value_type = value_converter._proto_class
        for _k, _v in _in.items():
            if type(_k) is not key_type:
                raise TypeError(f"expect key type={key_type}, get {type(_k)=}")
            if isinstance(_v, value_converter):
                res[_k] = _v
            elif isinstance(_v, _value_type):
                res[_k] = value_converter.convert(_v)
            else:
                raise TypeError
        return res

    @deprecated("use export_to_pb_msg_mapping_container instead")
    def export_pb(self) -> Dict[_K, Any]:
        return {_k: _v.export_pb() for _k, _v in self.items()}

    def export_to_pb(self, pb_msg: _pb_Message, field_name: str):
        _pb_container: Dict[_K, _pb_Message] = getattr(pb_msg, field_name)
        for _k, _v in self.items():
            _pb_container[_k].CopyFrom(_v.export_pb())

    # TODO: type checked dict API


class ScalarMapContainer(_MappingLikeContainerBase[_K, ScalarValueType]):
    def __init__(
        self,
        *,
        key_type: Type[_K],
        value_type: Type[ScalarValueType],
    ) -> None:
        self.key_type = key_type
        self.value_type = value_type

    @classmethod
    def convert(
        cls,
        _in: Mapping[_K, Any],
        /,
        key_type: Type[_K],
        value_type: Type[ScalarValueType],
    ) -> Self:
        res = cls(key_type=key_type, value_type=value_type)
        for _k, _v in _in.items():
            if type(_k) is not key_type:
                raise TypeError(f"expect key type={key_type}, get {type(_k)=}")
            if isinstance(_v, value_type):
                res[_k] = _v
            else:
                raise TypeError
        return res

    def export_pb(self) -> Dict[_K, Any]:
        return self.copy()

    def export_to_pb(self, pb_msg: _pb_Message, field_name: str):
        _pb_container: Dict[_K, ScalarValueType] = getattr(pb_msg, field_name)
        _pb_container.update(self)

    # TODO: type checked dict API


# field descriptor for MessageWrapper
#
# The descriptor implementation for different type of fields in
# message wrapper. Each descriptor stores information related to
# specific fields, including field converter type, etc.
#
# Each field will be assigned a default value if the assigned value is
# _DEFAULT_VALUE object.


_DEFAULT_VALUE = object()
_ATTR_PREFIX = "_attr_"


# real field name is occupied by the corresponding field descriptor,
# so we need to define different name for attrn when storing attr value.
def _get_field_attrn(_fname: str):
    return f"{_ATTR_PREFIX}{_fname}"


def _create_field_descriptor(field_annotation: Any) -> Optional[_FieldBase]:
    _origin_field_type = _reveal_origin_type(field_annotation)
    if _origin_field_type in SCALAR_VALUE_TYPES:
        return _ScalarValueField(field_annotation)
    elif issubclass(_origin_field_type, EnumWrapper):
        return _EnumField(field_annotation)
    elif issubclass(_origin_field_type, MessageWrapper):
        return _MessageField(field_annotation)
    elif issubclass(_origin_field_type, RepeatedCompositeContainer):
        return _RepeatedCompositeField(field_annotation)
    elif issubclass(_origin_field_type, RepeatedScalarContainer):
        return _RepeatedScalarField(field_annotation)
    elif issubclass(_origin_field_type, ScalarMapContainer):
        return _ScalarMappingField(field_annotation)
    elif issubclass(_origin_field_type, MessageMapContainer):
        return _MessageMappingField(field_annotation)


class _FieldBase(Generic[_T], ABC):
    """Base for message field descriptor.

    _T stands for the scalar value type for scalar value field,
        or converter type for non-scalar value field
        (message, enum, repeated field, etc).
    """

    @abstractmethod
    def __init__(self, field_annotation: Any) -> None: ...

    @overload
    def __get__(self, obj: None, objtype: type) -> Self:
        """Get descriptor."""

    @overload
    def __get__(self, obj, objtype: type) -> _T:
        """Get value from instance."""

    def __get__(self, obj, objtype=None) -> Union[Self, _T]:
        if obj is not None:
            return getattr(obj, self._attrn)  # access via instance
        return self  # access via class, return the descriptor itself

    @abstractmethod
    def __set__(self, obj, value: Any) -> None: ...

    def __set_name__(self, owner: type, name: str):
        """Being called when bound to class."""
        self.field_name = name
        self._attrn = _get_field_attrn(name)

    def export_to_pb(self, obj, pb_msg: _pb_Message):
        """Stub method for calling the underlaying wrapper types
        export_to_pb method.
        """
        _wrapper: WrapperBase = getattr(obj, self._attrn)
        _wrapper.export_to_pb(pb_msg, self.field_name)


class _ScalarValueField(_FieldBase[ScalarValueType]):
    def __init__(self, field_annotation: Any) -> None:
        self.field_type = _reveal_origin_type(field_annotation)

    def __set__(self, obj, value: Any) -> None:
        if value is _DEFAULT_VALUE:
            value = self.field_type()
        if not isinstance(value, self.field_type):
            raise TypeError
        setattr(obj, self._attrn, value)

    def export_to_pb(self, obj, pb_object: Any):
        # NOTE: no wrapper type for scalar field
        _attr_value = getattr(obj, self._attrn)
        setattr(pb_object, self.field_name, _attr_value)


class _MessageField(_FieldBase[MessageWrapperType]):
    """For field that contains one message wrapper inst."""

    def __init__(self, field_annotation: Any) -> None:
        self.field_type: Type[MessageWrapperType] = _reveal_origin_type(
            field_annotation
        )

    def __set__(self, obj, value: Any) -> None:
        # NOTE: type check is done by the converter
        if value is _DEFAULT_VALUE:
            value = self.field_type()
        else:
            value = self.field_type.convert(value)
        setattr(obj, self._attrn, value)


class _EnumField(_FieldBase[EnumWrapperType]):
    """For field that contains one enum value.

    Basically we can handle enum like handling a normal message instance,
        but parsing from/exporting to protobuf enum requires special treatment,
        so separate _EnumField descriptor is defined for enum field.
    """

    def __init__(self, field_annotation: Any) -> None:
        self.field_type: Type[EnumWrapperType] = _reveal_origin_type(field_annotation)

    def __set__(self, obj, value: Any) -> None:
        # NOTE: type check is done by the converter
        if value is _DEFAULT_VALUE:
            value = self.field_type()
        else:
            value = self.field_type.convert(value)
        setattr(obj, self._attrn, value)


class _ListLikeContainerField(_FieldBase[FieldContainerWrapperType]): ...


class _RepeatedCompositeField(_ListLikeContainerField):
    """
    Properly annotation for RepeatedCompositeField is as follow:
    RepeatedCompositeContainer[WrappedMessageType]
    """

    def __init__(self, field_annotation: Any) -> None:
        if not issubclass(
            _container_type := _reveal_origin_type(field_annotation),
            RepeatedCompositeContainer,
        ):
            raise TypeError(
                f"converter for repeated composite field should be: {RepeatedCompositeContainer}"
            )
        self.field_type = _container_type

        # parse type annotation to get container element type and its converter
        if len(_types_tuple := get_args(field_annotation)) != 1:
            raise TypeError(f"badly annotated repeated field: {field_annotation=}")
        _msg_converter_type = _reveal_origin_type(_types_tuple[0])
        if not issubclass(_msg_converter_type, MessageWrapper):
            raise TypeError(f"args[0] is not a proto converter: {_msg_converter_type}")
        self.element_wrapper_type = _msg_converter_type

    def __set__(self, obj, value: Any) -> None:
        if value is _DEFAULT_VALUE:
            value = self.field_type(converter_type=self.element_wrapper_type)
        else:
            value = self.field_type.convert(value, self.element_wrapper_type)
        setattr(obj, self._attrn, value)


class _RepeatedScalarField(_ListLikeContainerField):
    """
    Properly annotation for RepeatedScalarField is as follow:
    RepeatedScalarContainer[NormalType]
    """

    def __init__(self, field_annotation: Any) -> None:
        _container_type = _reveal_origin_type(field_annotation)
        if not issubclass(_container_type, RepeatedScalarContainer):
            raise TypeError(
                f"converter for repeated scalar field should be {RepeatedScalarContainer}"
            )
        self.field_type = _container_type

        # parse type annotation to get container element type
        if len(_types_tuple := get_args(field_annotation)) != 1:
            raise TypeError(f"badly annotated repeated field: {field_annotation=}")
        if (_element_type := _types_tuple[0]) not in SCALAR_VALUE_TYPES:
            raise TypeError(
                f"repeated scalar value field only takes: {SCALAR_VALUE_TYPES}"
            )
        self.element_type = _element_type

    def __set__(self, obj, value: Any) -> None:
        if value is _DEFAULT_VALUE:
            value = self.field_type(element_type=self.element_type)
        else:
            value = self.field_type.convert(value, self.element_type)
        setattr(obj, self._attrn, value)


class _MappingLikeContainerField(_FieldBase[FieldContainerWrapperType]): ...


class _MessageMappingField(_MappingLikeContainerField):
    """
    Proper type annotated message mapping field is as follow:
    MessageMapContainer[K, MessageWrapperType]
    """

    def __init__(self, field_annotation: Any) -> None:
        _container_type = _reveal_origin_type(field_annotation)
        if not issubclass(_container_type, MessageMapContainer):
            raise TypeError(
                f"converter for msg mapping field should be {MessageMapContainer}"
            )
        self.field_type = _container_type

        if len(_types_tuple := get_args(field_annotation)) != 2:
            raise TypeError(f"badly annotated mapping field: {field_annotation=}")
        _key_type, _value_wrapper_type = map(_reveal_origin_type, _types_tuple)
        if _key_type not in (int, str, bool):
            raise TypeError(f"key only allows: {int}, {str}, {bool}")
        if not issubclass(_value_wrapper_type, MessageWrapper):
            raise TypeError(f"args[1] is not a proto converter: {field_annotation}")
        self.key_type = _key_type
        self.value_wrapper_type = _value_wrapper_type

    def __set__(self, obj, value: Any) -> None:
        if value is _DEFAULT_VALUE:
            value = self.field_type(
                key_type=self.key_type, value_converter=self.value_wrapper_type
            )
        else:
            value = self.field_type.convert(
                value, self.key_type, self.value_wrapper_type
            )
        setattr(obj, self._attrn, value)


class _ScalarMappingField(_MappingLikeContainerField):
    """
    Proper type annotated scalar mapping field is as follow:
    ScalarMapContainer[K, ScalarValueType]
    """

    def __init__(self, field_annotation: Any) -> None:
        _container_type = _reveal_origin_type(field_annotation)
        if not issubclass(_container_type, ScalarMapContainer):
            raise TypeError(
                f"converter for scalar mapping field should be {ScalarMapContainer}"
            )
        self.field_type = _container_type

        if len(_types_tuple := get_args(field_annotation)) != 2:
            raise TypeError(f"badly annotated mapping field: {field_annotation=}")
        _key_type, _value_type = map(_reveal_origin_type, _types_tuple)
        if _key_type not in (int, str, bool):
            raise TypeError(f"key only allows: {int}, {str}, {bool}")
        if _value_type not in SCALAR_VALUE_TYPES:
            raise TypeError(f"args[1] must be scalar value type: {field_annotation}")
        self.key_type = _key_type
        self.value_type = _value_type

    def __set__(self, obj, value: Any) -> None:
        if value is _DEFAULT_VALUE:
            value = self.field_type(key_type=self.key_type, value_type=self.value_type)
        else:
            value = self.field_type.convert(value, self.key_type, self.value_type)
        setattr(obj, self._attrn, value)


# message wrapper base


class MessageWrapper(WrapperBase[MessageType]):
    _proto_class: Type[MessageType]
    _fields: List[str]
    __slots__: List[str]

    # internal

    def __init_subclass__(cls) -> None:
        """Special treatment for every user defined protobuf message wrapper types.

        - Parse type annotations defined in wrapper class.
        - bypass the user defined __init__.
        """
        if (
            not (_orig_bases := getattr(cls, "__orig_bases__", None))
            or len(_orig_bases) < 1
        ):
            raise TypeError("MessageWrapper should have type arg")
        # MessageWrapper should be the last in __mro__
        _typed_msg_wrapper = _orig_bases[-1]
        if len(_type_args_list := get_args(_typed_msg_wrapper)) != 1:
            raise TypeError("MessageWrapper is not properly typed")
        if not issubclass(_proto_msg_type := _type_args_list[0], _pb_Message):
            raise TypeError(
                f"MessageWrapper should wrap protobuf message, but get {_proto_msg_type}"
            )
        cls._proto_class = _proto_msg_type  # type: ignore

        # parse type hints
        cls._fields = []
        for _field_name, _field_annotation in get_type_hints(cls).items():
            if _field_name.startswith("_"):
                continue
            cls._fields.append(_field_name)

            # create field descriptor for each field from type annotation
            if not (_new_fd := _create_field_descriptor(_field_annotation)):
                raise ValueError(
                    f"bad annotation or unsupported type: {_field_name=}, {_field_annotation=}"
                )

            # NOTE: __set_name__ is called when the type creating new class,
            #       but we initialize field descriptors after class is created,
            #       we have to call the __set_name__ by ourselves
            _new_fd.__set_name__(cls, _field_name)
            setattr(cls, _field_name, _new_fd)

        # check slots, since we are using field descriptors for each field,
        # we expect the __slots__ is defined by provided calculate_slots method
        if "__slots__" in dir(cls):
            _slots = set(cls.__slots__)
            for _fn in cls._fields:
                _slots.discard(_get_field_attrn(_fn))
            if _slots:
                raise ValueError(
                    f"invalid slots detected: {_slots},"
                    "please use calculate_slots method to define __slots__,"
                    "or not define __slots__ at all"
                )

        # bypass user defined __init__ while preserving meta, including
        # function name, annotations, module, etc.
        def _dummy_init(self, /, **_): ...

        cls.__init__ = update_wrapper(_dummy_init, cls.__init__).__get__(cls)  # noqa

    def __new__(cls, /, **kwargs) -> Self:
        _inst = super().__new__(cls)
        for _field_name in cls._fields:
            # for unset/unpopulated field, a default value
            # will be assigned
            setattr(_inst, _field_name, kwargs.get(_field_name, _DEFAULT_VALUE))
        return _inst

    def __deepcopy__(self, memo=None) -> Self:
        _copied_fields = {}
        for _attrn in self._fields:
            _copied_fields[_attrn] = deepcopy(getattr(self, _attrn))
        return type(self)(**_copied_fields)

    def __getitem__(self, __name: str) -> Any:
        return getattr(self, __name)

    def __setitem__(self, __name: str, __value: Any):
        setattr(self, __name, __value)

    def __eq__(self, __o: object) -> bool:
        if self.__class__ != __o.__class__:
            return False
        return all(
            getattr(self, _field_name) == getattr(__o, _field_name)
            for _field_name in self._fields
        )

    def __str__(self) -> str:
        _buffer = StringIO()
        _buffer.write("{\n")
        for _field_name in self._fields:
            _attrv_str = str(getattr(self, _field_name))
            _buffer.write(f" {_field_name} :")
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
    def convert(cls, _in: Union[MessageType, Self, Mapping]) -> Self:
        """Copy and wrap input message into a new wrapper instance."""
        if isinstance(_in, cls):
            return _in  # do not re-convert again
        if isinstance(_in, Mapping):
            return cls(**_in)
        if isinstance(_in, cls._proto_class):
            _kwargs = {}
            for _field_name in cls._fields:
                _kwargs[_field_name] = getattr(_in, _field_name)
            return cls(**_kwargs)
        raise TypeError(
            f"expect {cls._proto_class}, {cls} or {Mapping}, get {_in.__class__}"
        )

    def export_pb(self) -> MessageType:
        """Export as protobuf message class inst."""
        _res = self._proto_class()
        for _field_name in self._fields:
            _fd: _FieldBase = getattr(self.__class__, _field_name)
            _fd.export_to_pb(self, _res)
        return _res

    def export_to_pb(self, pb_msg: _pb_Message, field_name: str):
        _nested_pb_msg: _pb_Message = getattr(pb_msg, field_name)
        _nested_pb_msg.CopyFrom(self.export_pb())

    def serialize_to_bytes(self) -> bytes:
        return self.export_pb().SerializeToString()

    @classmethod
    def converted_from_deserialized(cls, _bytes: bytes, /) -> Self:
        return cls.convert(cls._proto_class.FromString(_bytes))


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

    def export_to_pb(self, pb_msg: _pb_Message, field_name: str):
        setattr(pb_msg, field_name, self.export_pb())


WrapperBase.register(EnumWrapper)

# well-known types
# the converters for well-known type are implemented as special Message types
# check https://protobuf.dev/reference/python/python-generated/#wkt for details
# NOTE: currently only support Duration


class Duration(MessageWrapper[_Duration]):
    """Wrapper for protobuf well-known type Duration.

    NOTE: this wrapper supports directly adding nanoseconds.
    """

    __slots__ = calculate_slots(_Duration)
    seconds: int
    nanos: int
    _s2ns = 1_000_000_000

    def __init__(
        self, *, seconds: Optional[int] = ..., nanos: Optional[int] = ...
    ) -> None: ...

    @classmethod
    def from_nanoseconds(cls, _ns: int) -> Self:
        seconds, nanos = divmod(_ns, cls._s2ns)
        return cls(seconds=seconds, nanos=nanos)

    def add_nanoseconds(self, _ns: int):
        _add_seconds, _new_nanos = divmod(self.nanos + _ns, self._s2ns)
        self.seconds += _add_seconds
        self.nanos = _new_nanos
