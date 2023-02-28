from google.protobuf import duration_pb2 as _duration_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor
VALUE_0: SampleEnum
VALUE_1: SampleEnum
VALUE_2: SampleEnum

class InnerMessage(_message.Message):
    __slots__ = ["double_field", "duration_field", "enum_field", "int_field", "str_field"]
    DOUBLE_FIELD_FIELD_NUMBER: _ClassVar[int]
    DURATION_FIELD_FIELD_NUMBER: _ClassVar[int]
    ENUM_FIELD_FIELD_NUMBER: _ClassVar[int]
    INT_FIELD_FIELD_NUMBER: _ClassVar[int]
    STR_FIELD_FIELD_NUMBER: _ClassVar[int]
    double_field: float
    duration_field: _duration_pb2.Duration
    enum_field: SampleEnum
    int_field: int
    str_field: str
    def __init__(self, int_field: _Optional[int] = ..., double_field: _Optional[float] = ..., str_field: _Optional[str] = ..., duration_field: _Optional[_Union[_duration_pb2.Duration, _Mapping]] = ..., enum_field: _Optional[_Union[SampleEnum, str]] = ...) -> None: ...

class OuterMessage(_message.Message):
    __slots__ = ["mapping_composite_field", "mapping_scalar_field", "nested_msg", "repeated_composite_field", "repeated_scalar_field"]
    class MappingCompositeFieldEntry(_message.Message):
        __slots__ = ["key", "value"]
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: int
        value: InnerMessage
        def __init__(self, key: _Optional[int] = ..., value: _Optional[_Union[InnerMessage, _Mapping]] = ...) -> None: ...
    class MappingScalarFieldEntry(_message.Message):
        __slots__ = ["key", "value"]
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    MAPPING_COMPOSITE_FIELD_FIELD_NUMBER: _ClassVar[int]
    MAPPING_SCALAR_FIELD_FIELD_NUMBER: _ClassVar[int]
    NESTED_MSG_FIELD_NUMBER: _ClassVar[int]
    REPEATED_COMPOSITE_FIELD_FIELD_NUMBER: _ClassVar[int]
    REPEATED_SCALAR_FIELD_FIELD_NUMBER: _ClassVar[int]
    mapping_composite_field: _containers.MessageMap[int, InnerMessage]
    mapping_scalar_field: _containers.ScalarMap[str, str]
    nested_msg: InnerMessage
    repeated_composite_field: _containers.RepeatedCompositeFieldContainer[InnerMessage]
    repeated_scalar_field: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, repeated_scalar_field: _Optional[_Iterable[str]] = ..., repeated_composite_field: _Optional[_Iterable[_Union[InnerMessage, _Mapping]]] = ..., nested_msg: _Optional[_Union[InnerMessage, _Mapping]] = ..., mapping_scalar_field: _Optional[_Mapping[str, str]] = ..., mapping_composite_field: _Optional[_Mapping[int, InnerMessage]] = ...) -> None: ...

class SampleEnum(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = []
