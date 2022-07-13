from google.protobuf import duration_pb2 as _duration_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar, Iterable, Mapping, Optional, Text, Union

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
    ECU_FIELD_NUMBER: ClassVar[int]
    ecu: _containers.RepeatedCompositeFieldContainer[RollbackRequestEcu]
    def __init__(self, ecu: Optional[Iterable[Union[RollbackRequestEcu, Mapping]]] = ...) -> None: ...

class RollbackRequestEcu(_message.Message):
    __slots__ = ["ecu_id"]
    ECU_ID_FIELD_NUMBER: ClassVar[int]
    ecu_id: str
    def __init__(self, ecu_id: Optional[str] = ...) -> None: ...

class RollbackResponse(_message.Message):
    __slots__ = ["ecu"]
    ECU_FIELD_NUMBER: ClassVar[int]
    ecu: _containers.RepeatedCompositeFieldContainer[RollbackResponseEcu]
    def __init__(self, ecu: Optional[Iterable[Union[RollbackResponseEcu, Mapping]]] = ...) -> None: ...

class RollbackResponseEcu(_message.Message):
    __slots__ = ["ecu_id", "result"]
    ECU_ID_FIELD_NUMBER: ClassVar[int]
    RESULT_FIELD_NUMBER: ClassVar[int]
    ecu_id: str
    result: FailureType
    def __init__(self, ecu_id: Optional[str] = ..., result: Optional[Union[FailureType, str]] = ...) -> None: ...

class Status(_message.Message):
    __slots__ = ["failure", "failure_reason", "progress", "status", "version"]
    FAILURE_FIELD_NUMBER: ClassVar[int]
    FAILURE_REASON_FIELD_NUMBER: ClassVar[int]
    PROGRESS_FIELD_NUMBER: ClassVar[int]
    STATUS_FIELD_NUMBER: ClassVar[int]
    VERSION_FIELD_NUMBER: ClassVar[int]
    failure: FailureType
    failure_reason: str
    progress: StatusProgress
    status: StatusOta
    version: str
    def __init__(self, status: Optional[Union[StatusOta, str]] = ..., failure: Optional[Union[FailureType, str]] = ..., failure_reason: Optional[str] = ..., version: Optional[str] = ..., progress: Optional[Union[StatusProgress, Mapping]] = ...) -> None: ...

class StatusProgress(_message.Message):
    __slots__ = ["elapsed_time_copy", "elapsed_time_download", "elapsed_time_link", "errors_download", "file_size_processed_copy", "file_size_processed_download", "file_size_processed_link", "files_processed_copy", "files_processed_download", "files_processed_link", "phase", "regular_files_processed", "total_elapsed_time", "total_regular_file_size", "total_regular_files"]
    ELAPSED_TIME_COPY_FIELD_NUMBER: ClassVar[int]
    ELAPSED_TIME_DOWNLOAD_FIELD_NUMBER: ClassVar[int]
    ELAPSED_TIME_LINK_FIELD_NUMBER: ClassVar[int]
    ERRORS_DOWNLOAD_FIELD_NUMBER: ClassVar[int]
    FILES_PROCESSED_COPY_FIELD_NUMBER: ClassVar[int]
    FILES_PROCESSED_DOWNLOAD_FIELD_NUMBER: ClassVar[int]
    FILES_PROCESSED_LINK_FIELD_NUMBER: ClassVar[int]
    FILE_SIZE_PROCESSED_COPY_FIELD_NUMBER: ClassVar[int]
    FILE_SIZE_PROCESSED_DOWNLOAD_FIELD_NUMBER: ClassVar[int]
    FILE_SIZE_PROCESSED_LINK_FIELD_NUMBER: ClassVar[int]
    PHASE_FIELD_NUMBER: ClassVar[int]
    REGULAR_FILES_PROCESSED_FIELD_NUMBER: ClassVar[int]
    TOTAL_ELAPSED_TIME_FIELD_NUMBER: ClassVar[int]
    TOTAL_REGULAR_FILES_FIELD_NUMBER: ClassVar[int]
    TOTAL_REGULAR_FILE_SIZE_FIELD_NUMBER: ClassVar[int]
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
    def __init__(self, phase: Optional[Union[StatusProgressPhase, str]] = ..., total_regular_files: Optional[int] = ..., regular_files_processed: Optional[int] = ..., files_processed_copy: Optional[int] = ..., files_processed_link: Optional[int] = ..., files_processed_download: Optional[int] = ..., file_size_processed_copy: Optional[int] = ..., file_size_processed_link: Optional[int] = ..., file_size_processed_download: Optional[int] = ..., elapsed_time_copy: Optional[Union[_duration_pb2.Duration, Mapping]] = ..., elapsed_time_link: Optional[Union[_duration_pb2.Duration, Mapping]] = ..., elapsed_time_download: Optional[Union[_duration_pb2.Duration, Mapping]] = ..., errors_download: Optional[int] = ..., total_regular_file_size: Optional[int] = ..., total_elapsed_time: Optional[Union[_duration_pb2.Duration, Mapping]] = ...) -> None: ...

class StatusRequest(_message.Message):
    __slots__ = []
    def __init__(self) -> None: ...

class StatusResponse(_message.Message):
    __slots__ = ["available_ecu_ids", "ecu"]
    AVAILABLE_ECU_IDS_FIELD_NUMBER: ClassVar[int]
    ECU_FIELD_NUMBER: ClassVar[int]
    available_ecu_ids: _containers.RepeatedScalarFieldContainer[str]
    ecu: _containers.RepeatedCompositeFieldContainer[StatusResponseEcu]
    def __init__(self, ecu: Optional[Iterable[Union[StatusResponseEcu, Mapping]]] = ..., available_ecu_ids: Optional[Iterable[str]] = ...) -> None: ...

class StatusResponseEcu(_message.Message):
    __slots__ = ["ecu_id", "result", "status"]
    ECU_ID_FIELD_NUMBER: ClassVar[int]
    RESULT_FIELD_NUMBER: ClassVar[int]
    STATUS_FIELD_NUMBER: ClassVar[int]
    ecu_id: str
    result: FailureType
    status: Status
    def __init__(self, ecu_id: Optional[str] = ..., result: Optional[Union[FailureType, str]] = ..., status: Optional[Union[Status, Mapping]] = ...) -> None: ...

class UpdateRequest(_message.Message):
    __slots__ = ["ecu"]
    ECU_FIELD_NUMBER: ClassVar[int]
    ecu: _containers.RepeatedCompositeFieldContainer[UpdateRequestEcu]
    def __init__(self, ecu: Optional[Iterable[Union[UpdateRequestEcu, Mapping]]] = ...) -> None: ...

class UpdateRequestEcu(_message.Message):
    __slots__ = ["cookies", "ecu_id", "url", "version"]
    COOKIES_FIELD_NUMBER: ClassVar[int]
    ECU_ID_FIELD_NUMBER: ClassVar[int]
    URL_FIELD_NUMBER: ClassVar[int]
    VERSION_FIELD_NUMBER: ClassVar[int]
    cookies: str
    ecu_id: str
    url: str
    version: str
    def __init__(self, ecu_id: Optional[str] = ..., version: Optional[str] = ..., url: Optional[str] = ..., cookies: Optional[str] = ...) -> None: ...

class UpdateResponse(_message.Message):
    __slots__ = ["ecu"]
    ECU_FIELD_NUMBER: ClassVar[int]
    ecu: _containers.RepeatedCompositeFieldContainer[UpdateResponseEcu]
    def __init__(self, ecu: Optional[Iterable[Union[UpdateResponseEcu, Mapping]]] = ...) -> None: ...

class UpdateResponseEcu(_message.Message):
    __slots__ = ["ecu_id", "result"]
    ECU_ID_FIELD_NUMBER: ClassVar[int]
    RESULT_FIELD_NUMBER: ClassVar[int]
    ecu_id: str
    result: FailureType
    def __init__(self, ecu_id: Optional[str] = ..., result: Optional[Union[FailureType, str]] = ...) -> None: ...

class FailureType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = []

class StatusOta(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = []

class StatusProgressPhase(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = []
