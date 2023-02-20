# Protobuf message wrapper

A library that helps defining and creating wrappers for compiled generated protobuf message types.

## Feature

1. supports easily creating wrapper for any generated protobuf message types, custom helper methods can be defined for each created wrappers,
1. supports converting protobuf message instance into corresponding wrapper type instance, the converted wrapper instances can be used as normal python instances,
1. supports exporting wrapper instance back to protobuf message instance, the exported protobuf message instance is equal(each attribute fields have the same value) to the converted one at the beginning,
1. fully supports nested message, repeated fields and protobuf built-in well-known type(currently only `Duration` is supported),
1. fully inter-operatable with protobuf message with `convert` and `export_pb` API,
1. provided utils/types are well type hinted and annotated.

## Provided utils

A list of helper types/utils are provided to generate wrapper types from compiled protobuf message types:

- core utils/types

    | **name** | **description** |
    | --- | --- |
    | `MessageWrapper[<generated_pb_type>]` | the base class for all custom defined wrapper types |
    | `calculate_slots` | helper method to create `__slots__` for defined wrapper types |

- Pre-defined wrappers for special fields

    Currently only support repeated field.

    | **name** | **description** |
    | --- | --- |
    | `RepeatedCompositeContainer[<wrapper_type>]` | the wrapper for repeated field that contains messages |
    | `RepeatedScalarContainer[<normal_type>]` | the wrapper for repeated field that contains normal python types |

- wrappers for protobuf built-in well-known type

    Currenty only support `Duration` type.

    | **name** | **description** |
    | --- | --- |
    | `Duration` | the wrapper for protobuf well-known built-in type `Duration` |

## API

`MessageWrapper` and its subclasses have the following APIs:

```python
class MessageWrapper(Generic[_MessageType]):

    @classmethod
    @abstractmethod
    def convert(cls, _in: _MessageType) -> Self:
        """Convert a protobuf message inst and store in the wrapper inst."""

    @abstractmethod
    def export_pb(self) -> _MessageType:
        """Export the wrapper as a protobuf message inst. """
```

## HOWTO: define wrappers for generated protobuf message types

Wrappers for each defined protobuf message types can be simply created with type annotating based on the proto definition using the provided base types and helper methods.

### define wrappers based on proto definition

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

- Enum wrapper definition

    ```python
    from proto import EnumWrapper
    # import compiled protobuf code
    import my_proto_pb2 as _v2

    class StatusProgressPhase(EnumWrapper):
        INITIAL = _v2.INITIAL
        METADATA = _v2.METADATA
        DIRECTORY = _v2.DIRECTORY
        SYMLINK = _v2.SYMLINK
        REGULAR = _v2.REGULAR
        PERSISTENT = _v2.PERSISTENT
        POST_PROCESSING = _v2.POST_PROCESSING
    ```

- Message wrapper definition

    NOTE: the order of wrappers defined matters, for nested proto message type, the wrapper for included message must be defined before this nested message type.

    ```python
    from proto import MessageWrapper, Duration, calculate_slots
    import my_proto_pb2 as _v2

    # wrapper is recommended to have the same name
    # with the corresponding protobuf message
    class StatusProgress(MessageWrapper[_v2.StatusProgress]):
        # __slots__(OPTIONAL, RECOMMENDED)
        #   if __slots__ is needed, calculate_slots MUST be used
        #   to create the __slots__, otherwise don't define __slots__
        __slots__ = calculate_slots(_v2.StatusProgress)
        # fields type annotation(MUST)
        #   type annotations for each field according to the proto definition
        phase: StatusProgressPhase # use the custom wrapper defined above
        total_regular_files: int
        elapsed_time_download: Duration # use the provided wrapper
        # __init__ type hints(OPTIONAL, RECOMMENDED)
        #   type hints for each attributes when manually create instances
        #   of wrapper types.
        #   NOTE: this __init__ is just for typing use, it will not be
        #         used by the underlaying MessageWrapper
        #   NOTE: if pyi file is also generated when compiling the proto file,
        #         the __init__ in pyi file can be used directly here
        def __init__(
            self, *,
            phase: Optional[Union[StatusProgressPhase, str]] = ...,
            total_regular_files: Optional[int] = ...,
            elapsed_time_download: Optional[Duration] = ...,
        )

    # Status depends on StatusProgress, so it is defined after StatusProgress wrapper
    class Status(MessageWrapper[_v2.Status]):
        __slots__ = calculate_slots(_v2.Status)
        # for type hinting container fields like repeated,
        # use the wrapper container
        ecu_ids: RepeatedScalarContainer[str]
        progress: RepeatedCompositeContainer[StatusProgress]
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
