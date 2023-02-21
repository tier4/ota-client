# Protobuf message wrapper

A library that helps defining and creating wrappers for compiled generated protobuf message types.

## Feature

1. for unset/unpopulated field, aligns the behavior of protobuf message, wrapper will return the default value according to the field type(int, float will be 0, bool will be false, str will be empty string, **enum will be the first defined value in order**, **nested message will be empty message**).
1. supports **easily creating wrapper with type annotating only** for any generated protobuf message types , custom helper methods can be defined for each created wrappers,
1. **fully supports nested message, repeated fields and `Duration` type**,
1. **fully inter-operatable with protobuf message with `convert` and `export_pb` API,** supports converting protobuf message instance into corresponding wrapper type instance, the converted wrapper instances can be used as normal python instances,
1. simple and unified core APIs, supports exporting wrapper instance back to protobuf message instance, the exported protobuf message instance is equal(each attribute fields have the same value) to the converted one at the beginning,
1. fully type hinted and type annotated.

## Provided utils

A list of helper types/utils are provided to generate wrapper types from compiled protobuf message types:

- core utils/types

    | **name** | **description** | **usage**|
    | --- | --- | --- |
    | `MessageWrapper` | the base class for all custom defined wrapper types | define wrapper for compiled proto msg type `_pb2.Msg` by defining `Msg(MessageWrapper[_pb2.Msg])` |
    | `calculate_slots` | helper method to create `__slots__` for defined wrapper types | for compiled proto msg type `_pb2.Msg`, the `__slots__` can be defined by `__slots__ = calculate_slots(_pb2.Msg)` |

- Pre-defined wrappers for special fields

    NOTE: Currently only support repeated field.

    | **name** | **description** | **usage** |
    | --- | --- | --- |
    | `RepeatedCompositeContainer` | the wrapper for annotating repeated field that contains messages | for repeated field with proto msg type `_pb2.Msg` and its wrapper `Msg`, annotating the field as `RepeatedCompositeContainer[Msg]` |
    | `RepeatedScalarContainer` | the wrapper for repeated field that contains normal python types | for repeated field with type `str`, annotating the field as `RepeatedScalarContainer[str]` |

- wrappers for protobuf built-in well-known type

    NOTE: Currenty only support `Duration` type.

    | **name** | **description** | **usage** |
    | --- | --- | --- |
    | `Duration` | the wrapper for protobuf well-known built-in type `Duration` | annotating fields that are in protobuf well-known `Duration` type |

## API

`MessageWrapper` derived classes(detailed wrappers for specific proto message types), wrapper for well-known types and special fields(repeated, etc.) have the following APIs:

```python
class MessageWrapper(Generic[_MessageType]):

    @classmethod
    @abstractmethod
    def convert(cls, _in: _MessageType) -> Self:
        """Convert a protobuf message inst and store all attrs in the wrapper inst."""

    @abstractmethod
    def export_pb(self) -> _MessageType:
        """Export the wrapper as a protobuf message inst. """
```

## HOWTO: define wrappers for generated protobuf message types

Wrappers for each defined protobuf message types can be simply created with type annotating based on the proto definition using the provided base types and helper methods.

### define wrappers based on proto definition

- Consider the following proto file:

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

    ...

    msg: compiled_pb2.Status = ...
    # convert the message into corresponding wrapper instance
    converted = my_wrapper.Status.convert(msg) # type: my_wrapper.Status

    ```

1. export a wrapper instance and retrieve protobuf message instance

    ```python
    ...
    converted: my_wrapper.Status = ...
    msg: compiled_pb2.Status = converted.export_pb()
    ```
