from google.protobuf import duration_pb2 as _duration_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor
DIRECTORY: StatusProgressPhase
FAILURE: StatusOta
INITIAL: StatusProgressPhase
INITIALIZED: StatusOta
METADATA: StatusProgressPhase
NO_FAILURE: FailureType
PERSISTENT: StatusProgressPhase
POST_PROCESSING: StatusProgressPhase
RECOVERABLE: FailureType
REGULAR: StatusProgressPhase
ROLLBACKING: StatusOta
ROLLBACK_FAILURE: StatusOta
SUCCESS: StatusOta
SYMLINK: StatusProgressPhase
UNRECOVERABLE: FailureType
UPDATING: StatusOta

class RollbackRequest(_message.Message):
    __slots__ = ["ecu"]
    ECU_FIELD_NUMBER: _ClassVar[int]
    ecu: _containers.RepeatedCompositeFieldContainer[RollbackRequestEcu]
    def __init__(self, ecu: _Optional[_Iterable[_Union[RollbackRequestEcu, _Mapping]]] = ...) -> None: ...

class RollbackRequestEcu(_message.Message):
    __slots__ = ["ecu_id"]
    ECU_ID_FIELD_NUMBER: _ClassVar[int]
    ecu_id: str
    def __init__(self, ecu_id: _Optional[str] = ...) -> None: ...

class RollbackResponse(_message.Message):
    __slots__ = ["ecu"]
    ECU_FIELD_NUMBER: _ClassVar[int]
    ecu: _containers.RepeatedCompositeFieldContainer[RollbackResponseEcu]
    def __init__(self, ecu: _Optional[_Iterable[_Union[RollbackResponseEcu, _Mapping]]] = ...) -> None: ...

class RollbackResponseEcu(_message.Message):
    __slots__ = ["ecu_id", "result"]
    ECU_ID_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    ecu_id: str
    result: FailureType
    def __init__(self, ecu_id: _Optional[str] = ..., result: _Optional[_Union[FailureType, str]] = ...) -> None: ...

class Status(_message.Message):
    __slots__ = ["failure", "failure_reason", "progress", "status", "version"]
    FAILURE_FIELD_NUMBER: _ClassVar[int]
    FAILURE_REASON_FIELD_NUMBER: _ClassVar[int]
    PROGRESS_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    failure: FailureType
    failure_reason: str
    progress: StatusProgress
    status: StatusOta
    version: str
    def __init__(self, status: _Optional[_Union[StatusOta, str]] = ..., failure: _Optional[_Union[FailureType, str]] = ..., failure_reason: _Optional[str] = ..., version: _Optional[str] = ..., progress: _Optional[_Union[StatusProgress, _Mapping]] = ...) -> None: ...

class StatusProgress(_message.Message):
    __slots__ = ["download_bytes", "elapsed_time_copy", "elapsed_time_download", "elapsed_time_link", "errors_download", "file_size_processed_copy", "file_size_processed_download", "file_size_processed_link", "files_processed_copy", "files_processed_download", "files_processed_link", "phase", "regular_files_processed", "total_elapsed_time", "total_regular_file_size", "total_regular_files"]
    DOWNLOAD_BYTES_FIELD_NUMBER: _ClassVar[int]
    ELAPSED_TIME_COPY_FIELD_NUMBER: _ClassVar[int]
    ELAPSED_TIME_DOWNLOAD_FIELD_NUMBER: _ClassVar[int]
    ELAPSED_TIME_LINK_FIELD_NUMBER: _ClassVar[int]
    ERRORS_DOWNLOAD_FIELD_NUMBER: _ClassVar[int]
    FILES_PROCESSED_COPY_FIELD_NUMBER: _ClassVar[int]
    FILES_PROCESSED_DOWNLOAD_FIELD_NUMBER: _ClassVar[int]
    FILES_PROCESSED_LINK_FIELD_NUMBER: _ClassVar[int]
    FILE_SIZE_PROCESSED_COPY_FIELD_NUMBER: _ClassVar[int]
    FILE_SIZE_PROCESSED_DOWNLOAD_FIELD_NUMBER: _ClassVar[int]
    FILE_SIZE_PROCESSED_LINK_FIELD_NUMBER: _ClassVar[int]
    PHASE_FIELD_NUMBER: _ClassVar[int]
    REGULAR_FILES_PROCESSED_FIELD_NUMBER: _ClassVar[int]
    TOTAL_ELAPSED_TIME_FIELD_NUMBER: _ClassVar[int]
    TOTAL_REGULAR_FILES_FIELD_NUMBER: _ClassVar[int]
    TOTAL_REGULAR_FILE_SIZE_FIELD_NUMBER: _ClassVar[int]
    download_bytes: int
    elapsed_time_copy: _duration_pb2.Duration
    elapsed_time_download: _duration_pb2.Duration
    elapsed_time_link: _duration_pb2.Duration
    errors_download: int
    file_size_processed_copy: int
    file_size_processed_download: int
    file_size_processed_link: int
    files_processed_copy: int
    files_processed_download: int
    files_processed_link: int
    phase: StatusProgressPhase
    regular_files_processed: int
    total_elapsed_time: _duration_pb2.Duration
    total_regular_file_size: int
    total_regular_files: int
    def __init__(self, phase: _Optional[_Union[StatusProgressPhase, str]] = ..., total_regular_files: _Optional[int] = ..., regular_files_processed: _Optional[int] = ..., files_processed_copy: _Optional[int] = ..., files_processed_link: _Optional[int] = ..., files_processed_download: _Optional[int] = ..., file_size_processed_copy: _Optional[int] = ..., file_size_processed_link: _Optional[int] = ..., file_size_processed_download: _Optional[int] = ..., elapsed_time_copy: _Optional[_Union[_duration_pb2.Duration, _Mapping]] = ..., elapsed_time_link: _Optional[_Union[_duration_pb2.Duration, _Mapping]] = ..., elapsed_time_download: _Optional[_Union[_duration_pb2.Duration, _Mapping]] = ..., errors_download: _Optional[int] = ..., total_regular_file_size: _Optional[int] = ..., total_elapsed_time: _Optional[_Union[_duration_pb2.Duration, _Mapping]] = ..., download_bytes: _Optional[int] = ...) -> None: ...

class StatusRequest(_message.Message):
    __slots__ = []
    def __init__(self) -> None: ...

class StatusResponse(_message.Message):
    __slots__ = ["available_ecu_ids", "ecu"]
    AVAILABLE_ECU_IDS_FIELD_NUMBER: _ClassVar[int]
    ECU_FIELD_NUMBER: _ClassVar[int]
    available_ecu_ids: _containers.RepeatedScalarFieldContainer[str]
    ecu: _containers.RepeatedCompositeFieldContainer[StatusResponseEcu]
    def __init__(self, ecu: _Optional[_Iterable[_Union[StatusResponseEcu, _Mapping]]] = ..., available_ecu_ids: _Optional[_Iterable[str]] = ...) -> None: ...

class StatusResponseEcu(_message.Message):
    __slots__ = ["ecu_id", "result", "status"]
    ECU_ID_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ecu_id: str
    result: FailureType
    status: Status
    def __init__(self, ecu_id: _Optional[str] = ..., result: _Optional[_Union[FailureType, str]] = ..., status: _Optional[_Union[Status, _Mapping]] = ...) -> None: ...

class UpdateRequest(_message.Message):
    __slots__ = ["ecu"]
    ECU_FIELD_NUMBER: _ClassVar[int]
    ecu: _containers.RepeatedCompositeFieldContainer[UpdateRequestEcu]
    def __init__(self, ecu: _Optional[_Iterable[_Union[UpdateRequestEcu, _Mapping]]] = ...) -> None: ...

class UpdateRequestEcu(_message.Message):
    __slots__ = ["cookies", "ecu_id", "url", "version"]
    COOKIES_FIELD_NUMBER: _ClassVar[int]
    ECU_ID_FIELD_NUMBER: _ClassVar[int]
    URL_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    cookies: str
    ecu_id: str
    url: str
    version: str
    def __init__(self, ecu_id: _Optional[str] = ..., version: _Optional[str] = ..., url: _Optional[str] = ..., cookies: _Optional[str] = ...) -> None: ...

class UpdateResponse(_message.Message):
    __slots__ = ["ecu"]
    ECU_FIELD_NUMBER: _ClassVar[int]
    ecu: _containers.RepeatedCompositeFieldContainer[UpdateResponseEcu]
    def __init__(self, ecu: _Optional[_Iterable[_Union[UpdateResponseEcu, _Mapping]]] = ...) -> None: ...

class UpdateResponseEcu(_message.Message):
    __slots__ = ["ecu_id", "result"]
    ECU_ID_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    ecu_id: str
    result: FailureType
    def __init__(self, ecu_id: _Optional[str] = ..., result: _Optional[_Union[FailureType, str]] = ...) -> None: ...

class FailureType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = []

class StatusOta(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = []

class StatusProgressPhase(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = []
