from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class LogType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = []
    LOG: _ClassVar[LogType]
    METRICS: _ClassVar[LogType]

class LogLevel(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = []
    UNSPECIFIC: _ClassVar[LogLevel]
    TRACE: _ClassVar[LogLevel]
    DEBUG: _ClassVar[LogLevel]
    INFO: _ClassVar[LogLevel]
    WARN: _ClassVar[LogLevel]
    ERROR: _ClassVar[LogLevel]
    FATAL: _ClassVar[LogLevel]

class ErrorCode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = []
    NO_FAILURE: _ClassVar[ErrorCode]
    SERVER_QUEUE_FULL: _ClassVar[ErrorCode]
    NOT_ALLOWED_ECU_ID: _ClassVar[ErrorCode]
    NO_MESSAGE: _ClassVar[ErrorCode]

LOG: LogType
METRICS: LogType
UNSPECIFIC: LogLevel
TRACE: LogLevel
DEBUG: LogLevel
INFO: LogLevel
WARN: LogLevel
ERROR: LogLevel
FATAL: LogLevel
NO_FAILURE: ErrorCode
SERVER_QUEUE_FULL: ErrorCode
NOT_ALLOWED_ECU_ID: ErrorCode
NO_MESSAGE: ErrorCode

class PutLogRequest(_message.Message):
    __slots__ = ["ecu_id", "log_type", "timestamp", "level", "message"]
    ECU_ID_FIELD_NUMBER: _ClassVar[int]
    LOG_TYPE_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    LEVEL_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    ecu_id: str
    log_type: LogType
    timestamp: int
    level: LogLevel
    message: str
    def __init__(
        self,
        ecu_id: _Optional[str] = ...,
        log_type: _Optional[_Union[LogType, str]] = ...,
        timestamp: _Optional[int] = ...,
        level: _Optional[_Union[LogLevel, str]] = ...,
        message: _Optional[str] = ...,
    ) -> None: ...

class PutLogResponse(_message.Message):
    __slots__ = ["code", "message"]
    CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    code: ErrorCode
    message: str
    def __init__(
        self,
        code: _Optional[_Union[ErrorCode, str]] = ...,
        message: _Optional[str] = ...,
    ) -> None: ...
