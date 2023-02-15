# Protobuf message wrapper

A library that helps defining wrappers for compiled generated protobuf message types.

## Feature

1. supports easily creating wrapper for any generated protobuf message types,
1. supports converting any protobuf message instance and store all attributes in corresponding wrapper type instance,
1. supports exporting wrapper instance back to protobuf message instance, the exported protobuf message instance is equal(each attribute fields have the same value) to the converted one at the beginning,
1. fully supports nested message, repeated fields and protobuf built-in well-known type(currently only `Duration` is supported),
1. fully inter-operatable with protobuf message by `convert` and `export_pb` API,
1. provided utils/types are well type hinted and annotated.

## Provided utils

A list of helper types/utils are provided to generate wrapper types from compiled protobuf message types:

- common utils

    | **name** | **description** |
    | --- | --- |
    | `TypeConverterRegister` | register the defined wrapper to corresponding generated protobuf message type |
    | `MessageWrapper[<generated_pb_type>]` | the base class for all wrapper type |

- Pre-defined wrapper

    | **name** | **description** |
    | --- | --- |
    | `DurationWrapper` | the wrapper for protobuf well-known built-in type `Duration`, store duration as nanoseconds with int internally |
    | `RepeatedCompositeContainer[<wrapper_type>, <generated_pb_type>]` | the wrapper for repeated field that contains messages |
    | `RepeatedScalarContainer[<normal_type>]` | the wrapper for repeated field that contains normal python types |

## API

`MessageWrapper` and its subclasses have the following APIs:

```python
class MessageWrapper(Generic[_MessageType])
    proto_class: Type[_MessageType]

    @classmethod
    @abstractmethod
    def convert(cls, _in: _MessageType) -> Self:
        """Convert a protobuf message inst and store in the wrapper inst."""

    @abstractmethod
    def export_pb(self) -> _MessageType:
        """Export the wrapper as a protobuf message inst. """
```

Wrapper types register `TypeConverterRegister` has the following APIs:

```python
class TypeConverterRegister:
    TYPE_CONVERTER_REGISTER = {}

    @classmethod
    def register(cls, message_type, converter_type) -> None:
        """Register a wrapper to a specific generated protobuf message type."""

    @classmethod
    def get_converter(
        cls, message_type, default=None
    ) -> Optional[Type[_ProtobufConverter[Any]]]:
        """Get converter for the generated protobuf message type <message_type>."""
```

## HOWTO: define wrappers for generated protobuf message types

### define & compile proto file

Consider the following proto file:

```protobuf
// my_proto.proto

syntax = "proto3";

...

enum StatusProgressPhase {
  INITIAL = 0;
  METADATA = 1;
  DIRECTORY = 2;
  SYMLINK = 3;
  REGULAR = 4;
  PERSISTENT = 5;
  POST_PROCESSING = 6;
}

...

message StatusProgress {
  StatusProgressPhase phase = 1;
  uint64 total_regular_files = 2;
  google.protobuf.Duration elapsed_time_download = 12;
}

message Status {
  repeated string ecu_ids = 4;
  repeated StatusProgress progress = 5;
}
```

Compile the above proto file, we will have the following pyi file generated:

```python
# real module: my_proto_pb2.py
# pyi file: my_proto_pb2.pyi
...
from google.protobuf import duration_pb2 as _duration_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
...

INITIAL: StatusProgressPhase
METADATA: StatusProgressPhase
PERSISTENT: StatusProgressPhase
POST_PROCESSING: StatusProgressPhase
REGULAR: StatusProgressPhase
SYMLINK: StatusProgressPhase

...

class Status(_message.Message):
    __slots__ = ["progress", "ecu_ids"]
    STATUS_FIELD_NUMBER: ClassVar[int]
    VERSION_FIELD_NUMBER: ClassVar[int]
    progress: _containers.RepeatedCompositeFieldContainer[StatusProgress]
    version: _containers.RepeatedScalarFieldContainer[str]
    def __init__(
        self, 
        version: Optional[Iterable[str]] = ..., 
        progress: Optional[Iterable[Union[StatusProgress, Mapping]]] = ...) -> None: ...

class StatusProgress(_message.Message):
    __slots__ = [..., "phase", "total_regular_files", "elapsed_time_download"]
    ELAPSED_TIME_DOWNLOAD_FIELD_NUMBER: ClassVar[int]
    PHASE_FIELD_NUMBER: ClassVar[int]
    TOTAL_REGULAR_FILES_FIELD_NUMBER: ClassVar[int]
    elapsed_time_download: _duration_pb2.Duration
    phase: StatusProgressPhase
    total_regular_files: int

    def __init__(self, phase: Optional[Union[StatusProgressPhase, str]] = ..., total_regular_files: Optional[int] = ..., elapsed_time_download: Optional[Union[_duration_pb2.Duration, Mapping]] = ...) -> None: ...


class StatusProgressPhase(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = []
```

## define wrapper class

Using the helper types/utils provided by this module, we can define the wrapper types as follow:

```python
import my_proto_pb2 as compiled_pb2
from enum import IntEnum
from proto._common import (
    TypeConverterRegister as _register,
    MessageWrapper,
    RepeatedCompositeContainer,
    RepeatedScalarContainer,
)

# enum definition

# group protobuf enums from the same protobuf enum class 
#   into a IntEnum class with same name.
class StatusProgressPhase(IntEnum):
    INITIAL = compiled_pb2.INITIAL
    METADATA = compiled_pb2.METADATA
    DIRECTORY = compiled_pb2.DIRECTORY
    SYMLINK = compiled_pb2.SYMLINK
    REGULAR = compiled_pb2.REGULAR
    PERSISTENT = compiled_pb2.PERSISTENT
    POST_PROCESSING = pcompiled_pb2b2.POST_PROCESSING

# message wrapper definition

# wrapper should have the same name as the corresponding 
#   protobuf message class.
class Status(MessageWrapper[compiled_pb2.Status]):
    proto_class = compiled_pb2.Status
    __slots__ = list(compiled_pb2.Status.DESCRIPTOR.fields_by_name)
    # copy the attributes definition from compiled pyi protobuf type
    #
    # type hint the enum/nested/contained message with corresponding wrapper type
    # NOTE: for repeated field, 
    #   - if repeated field contains message(composite repeated field), use 
    #     the RepeatedCompositeContainer wrapper type to annotate, the first args
    #     is the wrapper type, the second args is the corresponding wrapped 
    #     compiled protobuf message type.
    progress: RepeatedCompositeContainer[StatusProgress, compiled_pb2.StatusProgress]
    #   - if repeated field contains normal types(scalar repeated field), use
    #     the RepeatedScalarContainer wrapper type to annotate,
    version: RepeatedScalarContainer[str]

    # directly copy the __init__ method from compiled pyi protobuf type
    # NOTE: this __init__ method is only for type hinting, so
    #       leave the method body empty.
    def __init__(
        self, 
        version: Optional[Iterable[str]] = ..., 
        progress: Optional[Iterable[Union[StatusProgress, Mapping]]] = ...) -> None: ...


    # custom helper methods can be defined below
    def status_msg_helper_method(self):
        ...

    def status_msg_helper_method2(self, *args, **kwargs):
        ...

class StatusProgress(MessageWrapper[compiled_pb2.StatusProgress]):
    proto_class = compiled_pb2.StatusProgress
    __slots__ = list(compiled_pb2.StatusProgress.DESCRIPTOR.fields_by_name)
    # for Duration protobuf type, use the DurationWrapper to annotate
    elapsed_time_download: DurationWrapper
    # for field contains message/enum/common types, directly use the 
    # original protobuf type name
    phase: StatusProgressPhase
    total_regular_files: int

    # directly copy the __init__ method from compiled pyi protobuf type
    def __init__(
        self,
        phase: Optional[Union[StatusProgressPhase, str]] = ...,
        total_regular_files: Optional[int] = ...,
        elapsed_time_download: Optional[DurationWrapper] = ...,
    ) -> None:
        ...

    # custom helper methods can be defined below
    def get_snapshot(self) -> StatusProgress:
        ...

# finally, register the above defined wrapper types to the corresponding
# compiled protobuf message type
_register.register(compiled_pb2.Status, Status)
_register.register(compiled_pb2.StatusProgress, StatusProgress)


```

## HOWTO: usage of wrapper type

With the previous chapter's defined wrapper, how we can use them is as follow.

1. convert a protobuf message instance and turn into a wrapper instance

    ```python
    import compiled_pb2
    # wrappers are defined in my_wrapper module
    import my_wrapper
    from proto._common import TypeConverterRegister as _register

    ...

    msg: compiled_pb2.Status = ...
    # get the converter from the register
    converter = _register.get_converter(type(msg))
    # convert the message into corresponding wrapper instance
    converted = converter.convert(msg) # type: my_wrapper.Status

    ```

1. export a wrapper instance and retrieve protobuf message instance

    ```python
    ...
    converted: my_wrapper.Status = ...
    msg: compiled_pb2.Status = converted.export_pb()
    ```
