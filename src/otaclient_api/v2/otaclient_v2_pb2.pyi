from google.protobuf import duration_pb2 as _duration_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class FailureType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = []
    NO_FAILURE: _ClassVar[FailureType]
    RECOVERABLE: _ClassVar[FailureType]
    UNRECOVERABLE: _ClassVar[FailureType]

class StatusOta(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = []
    INITIALIZED: _ClassVar[StatusOta]
    SUCCESS: _ClassVar[StatusOta]
    FAILURE: _ClassVar[StatusOta]
    UPDATING: _ClassVar[StatusOta]
    ROLLBACKING: _ClassVar[StatusOta]
    ROLLBACK_FAILURE: _ClassVar[StatusOta]

class StatusProgressPhase(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = []
    INITIAL: _ClassVar[StatusProgressPhase]
    METADATA: _ClassVar[StatusProgressPhase]
    DIRECTORY: _ClassVar[StatusProgressPhase]
    SYMLINK: _ClassVar[StatusProgressPhase]
    REGULAR: _ClassVar[StatusProgressPhase]
    PERSISTENT: _ClassVar[StatusProgressPhase]
    POST_PROCESSING: _ClassVar[StatusProgressPhase]

class UpdatePhase(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = []
    INITIALIZING: _ClassVar[UpdatePhase]
    PROCESSING_METADATA: _ClassVar[UpdatePhase]
    CALCULATING_DELTA: _ClassVar[UpdatePhase]
    DOWNLOADING_OTA_FILES: _ClassVar[UpdatePhase]
    APPLYING_UPDATE: _ClassVar[UpdatePhase]
    PROCESSING_POSTUPDATE: _ClassVar[UpdatePhase]
    FINALIZING_UPDATE: _ClassVar[UpdatePhase]
NO_FAILURE: FailureType
RECOVERABLE: FailureType
UNRECOVERABLE: FailureType
INITIALIZED: StatusOta
SUCCESS: StatusOta
FAILURE: StatusOta
UPDATING: StatusOta
ROLLBACKING: StatusOta
ROLLBACK_FAILURE: StatusOta
INITIAL: StatusProgressPhase
METADATA: StatusProgressPhase
DIRECTORY: StatusProgressPhase
SYMLINK: StatusProgressPhase
REGULAR: StatusProgressPhase
PERSISTENT: StatusProgressPhase
POST_PROCESSING: StatusProgressPhase
INITIALIZING: UpdatePhase
PROCESSING_METADATA: UpdatePhase
CALCULATING_DELTA: UpdatePhase
DOWNLOADING_OTA_FILES: UpdatePhase
APPLYING_UPDATE: UpdatePhase
PROCESSING_POSTUPDATE: UpdatePhase
FINALIZING_UPDATE: UpdatePhase

class UpdateRequestEcu(_message.Message):
    __slots__ = ["ecu_id", "version", "url", "cookies"]
    ECU_ID_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    URL_FIELD_NUMBER: _ClassVar[int]
    COOKIES_FIELD_NUMBER: _ClassVar[int]
    ecu_id: str
    version: str
    url: str
    cookies: str
    def __init__(self, ecu_id: _Optional[str] = ..., version: _Optional[str] = ..., url: _Optional[str] = ..., cookies: _Optional[str] = ...) -> None: ...

class UpdateRequest(_message.Message):
    __slots__ = ["ecu"]
    ECU_FIELD_NUMBER: _ClassVar[int]
    ecu: _containers.RepeatedCompositeFieldContainer[UpdateRequestEcu]
    def __init__(self, ecu: _Optional[_Iterable[_Union[UpdateRequestEcu, _Mapping]]] = ...) -> None: ...

class UpdateResponseEcu(_message.Message):
    __slots__ = ["ecu_id", "result"]
    ECU_ID_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    ecu_id: str
    result: FailureType
    def __init__(self, ecu_id: _Optional[str] = ..., result: _Optional[_Union[FailureType, str]] = ...) -> None: ...

class UpdateResponse(_message.Message):
    __slots__ = ["ecu"]
    ECU_FIELD_NUMBER: _ClassVar[int]
    ecu: _containers.RepeatedCompositeFieldContainer[UpdateResponseEcu]
    def __init__(self, ecu: _Optional[_Iterable[_Union[UpdateResponseEcu, _Mapping]]] = ...) -> None: ...

class RollbackRequestEcu(_message.Message):
    __slots__ = ["ecu_id"]
    ECU_ID_FIELD_NUMBER: _ClassVar[int]
    ecu_id: str
    def __init__(self, ecu_id: _Optional[str] = ...) -> None: ...

class RollbackRequest(_message.Message):
    __slots__ = ["ecu"]
    ECU_FIELD_NUMBER: _ClassVar[int]
    ecu: _containers.RepeatedCompositeFieldContainer[RollbackRequestEcu]
    def __init__(self, ecu: _Optional[_Iterable[_Union[RollbackRequestEcu, _Mapping]]] = ...) -> None: ...

class RollbackResponseEcu(_message.Message):
    __slots__ = ["ecu_id", "result"]
    ECU_ID_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    ecu_id: str
    result: FailureType
    def __init__(self, ecu_id: _Optional[str] = ..., result: _Optional[_Union[FailureType, str]] = ...) -> None: ...

class RollbackResponse(_message.Message):
    __slots__ = ["ecu"]
    ECU_FIELD_NUMBER: _ClassVar[int]
    ecu: _containers.RepeatedCompositeFieldContainer[RollbackResponseEcu]
    def __init__(self, ecu: _Optional[_Iterable[_Union[RollbackResponseEcu, _Mapping]]] = ...) -> None: ...

class StatusRequest(_message.Message):
    __slots__ = []
    def __init__(self) -> None: ...

class StatusProgress(_message.Message):
    __slots__ = ["phase", "total_regular_files", "regular_files_processed", "files_processed_copy", "files_processed_link", "files_processed_download", "file_size_processed_copy", "file_size_processed_link", "file_size_processed_download", "elapsed_time_copy", "elapsed_time_link", "elapsed_time_download", "errors_download", "total_regular_file_size", "total_elapsed_time", "download_bytes"]
    PHASE_FIELD_NUMBER: _ClassVar[int]
    TOTAL_REGULAR_FILES_FIELD_NUMBER: _ClassVar[int]
    REGULAR_FILES_PROCESSED_FIELD_NUMBER: _ClassVar[int]
    FILES_PROCESSED_COPY_FIELD_NUMBER: _ClassVar[int]
    FILES_PROCESSED_LINK_FIELD_NUMBER: _ClassVar[int]
    FILES_PROCESSED_DOWNLOAD_FIELD_NUMBER: _ClassVar[int]
    FILE_SIZE_PROCESSED_COPY_FIELD_NUMBER: _ClassVar[int]
    FILE_SIZE_PROCESSED_LINK_FIELD_NUMBER: _ClassVar[int]
    FILE_SIZE_PROCESSED_DOWNLOAD_FIELD_NUMBER: _ClassVar[int]
    ELAPSED_TIME_COPY_FIELD_NUMBER: _ClassVar[int]
    ELAPSED_TIME_LINK_FIELD_NUMBER: _ClassVar[int]
    ELAPSED_TIME_DOWNLOAD_FIELD_NUMBER: _ClassVar[int]
    ERRORS_DOWNLOAD_FIELD_NUMBER: _ClassVar[int]
    TOTAL_REGULAR_FILE_SIZE_FIELD_NUMBER: _ClassVar[int]
    TOTAL_ELAPSED_TIME_FIELD_NUMBER: _ClassVar[int]
    DOWNLOAD_BYTES_FIELD_NUMBER: _ClassVar[int]
    phase: StatusProgressPhase
    total_regular_files: int
    regular_files_processed: int
    files_processed_copy: int
    files_processed_link: int
    files_processed_download: int
    file_size_processed_copy: int
    file_size_processed_link: int
    file_size_processed_download: int
    elapsed_time_copy: _duration_pb2.Duration
    elapsed_time_link: _duration_pb2.Duration
    elapsed_time_download: _duration_pb2.Duration
    errors_download: int
    total_regular_file_size: int
    total_elapsed_time: _duration_pb2.Duration
    download_bytes: int
    def __init__(self, phase: _Optional[_Union[StatusProgressPhase, str]] = ..., total_regular_files: _Optional[int] = ..., regular_files_processed: _Optional[int] = ..., files_processed_copy: _Optional[int] = ..., files_processed_link: _Optional[int] = ..., files_processed_download: _Optional[int] = ..., file_size_processed_copy: _Optional[int] = ..., file_size_processed_link: _Optional[int] = ..., file_size_processed_download: _Optional[int] = ..., elapsed_time_copy: _Optional[_Union[_duration_pb2.Duration, _Mapping]] = ..., elapsed_time_link: _Optional[_Union[_duration_pb2.Duration, _Mapping]] = ..., elapsed_time_download: _Optional[_Union[_duration_pb2.Duration, _Mapping]] = ..., errors_download: _Optional[int] = ..., total_regular_file_size: _Optional[int] = ..., total_elapsed_time: _Optional[_Union[_duration_pb2.Duration, _Mapping]] = ..., download_bytes: _Optional[int] = ...) -> None: ...

class Status(_message.Message):
    __slots__ = ["status", "failure", "failure_reason", "version", "progress"]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    FAILURE_FIELD_NUMBER: _ClassVar[int]
    FAILURE_REASON_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    PROGRESS_FIELD_NUMBER: _ClassVar[int]
    status: StatusOta
    failure: FailureType
    failure_reason: str
    version: str
    progress: StatusProgress
    def __init__(self, status: _Optional[_Union[StatusOta, str]] = ..., failure: _Optional[_Union[FailureType, str]] = ..., failure_reason: _Optional[str] = ..., version: _Optional[str] = ..., progress: _Optional[_Union[StatusProgress, _Mapping]] = ...) -> None: ...

class StatusResponseEcu(_message.Message):
    __slots__ = ["ecu_id", "result", "status"]
    ECU_ID_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ecu_id: str
    result: FailureType
    status: Status
    def __init__(self, ecu_id: _Optional[str] = ..., result: _Optional[_Union[FailureType, str]] = ..., status: _Optional[_Union[Status, _Mapping]] = ...) -> None: ...

class StatusResponse(_message.Message):
    __slots__ = ["ecu", "available_ecu_ids", "ecu_v2"]
    ECU_FIELD_NUMBER: _ClassVar[int]
    AVAILABLE_ECU_IDS_FIELD_NUMBER: _ClassVar[int]
    ECU_V2_FIELD_NUMBER: _ClassVar[int]
    ecu: _containers.RepeatedCompositeFieldContainer[StatusResponseEcu]
    available_ecu_ids: _containers.RepeatedScalarFieldContainer[str]
    ecu_v2: _containers.RepeatedCompositeFieldContainer[StatusResponseEcuV2]
    def __init__(self, ecu: _Optional[_Iterable[_Union[StatusResponseEcu, _Mapping]]] = ..., available_ecu_ids: _Optional[_Iterable[str]] = ..., ecu_v2: _Optional[_Iterable[_Union[StatusResponseEcuV2, _Mapping]]] = ...) -> None: ...

class StatusResponseEcuV2(_message.Message):
    __slots__ = ["ecu_id", "firmware_version", "otaclient_version", "ota_status", "failure_type", "failure_reason", "failure_traceback", "update_status"]
    ECU_ID_FIELD_NUMBER: _ClassVar[int]
    FIRMWARE_VERSION_FIELD_NUMBER: _ClassVar[int]
    OTACLIENT_VERSION_FIELD_NUMBER: _ClassVar[int]
    OTA_STATUS_FIELD_NUMBER: _ClassVar[int]
    FAILURE_TYPE_FIELD_NUMBER: _ClassVar[int]
    FAILURE_REASON_FIELD_NUMBER: _ClassVar[int]
    FAILURE_TRACEBACK_FIELD_NUMBER: _ClassVar[int]
    UPDATE_STATUS_FIELD_NUMBER: _ClassVar[int]
    ecu_id: str
    firmware_version: str
    otaclient_version: str
    ota_status: StatusOta
    failure_type: FailureType
    failure_reason: str
    failure_traceback: str
    update_status: UpdateStatus
    def __init__(self, ecu_id: _Optional[str] = ..., firmware_version: _Optional[str] = ..., otaclient_version: _Optional[str] = ..., ota_status: _Optional[_Union[StatusOta, str]] = ..., failure_type: _Optional[_Union[FailureType, str]] = ..., failure_reason: _Optional[str] = ..., failure_traceback: _Optional[str] = ..., update_status: _Optional[_Union[UpdateStatus, _Mapping]] = ...) -> None: ...

class UpdateStatus(_message.Message):
    __slots__ = ["update_firmware_version", "total_files_size_uncompressed", "total_files_num", "update_start_timestamp", "phase", "total_download_files_num", "total_download_files_size", "downloaded_files_num", "downloaded_bytes", "downloaded_files_size", "downloading_errors", "total_remove_files_num", "removed_files_num", "processed_files_num", "processed_files_size", "total_elapsed_time", "delta_generating_elapsed_time", "downloading_elapsed_time", "update_applying_elapsed_time"]
    UPDATE_FIRMWARE_VERSION_FIELD_NUMBER: _ClassVar[int]
    TOTAL_FILES_SIZE_UNCOMPRESSED_FIELD_NUMBER: _ClassVar[int]
    TOTAL_FILES_NUM_FIELD_NUMBER: _ClassVar[int]
    UPDATE_START_TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    PHASE_FIELD_NUMBER: _ClassVar[int]
    TOTAL_DOWNLOAD_FILES_NUM_FIELD_NUMBER: _ClassVar[int]
    TOTAL_DOWNLOAD_FILES_SIZE_FIELD_NUMBER: _ClassVar[int]
    DOWNLOADED_FILES_NUM_FIELD_NUMBER: _ClassVar[int]
    DOWNLOADED_BYTES_FIELD_NUMBER: _ClassVar[int]
    DOWNLOADED_FILES_SIZE_FIELD_NUMBER: _ClassVar[int]
    DOWNLOADING_ERRORS_FIELD_NUMBER: _ClassVar[int]
    TOTAL_REMOVE_FILES_NUM_FIELD_NUMBER: _ClassVar[int]
    REMOVED_FILES_NUM_FIELD_NUMBER: _ClassVar[int]
    PROCESSED_FILES_NUM_FIELD_NUMBER: _ClassVar[int]
    PROCESSED_FILES_SIZE_FIELD_NUMBER: _ClassVar[int]
    TOTAL_ELAPSED_TIME_FIELD_NUMBER: _ClassVar[int]
    DELTA_GENERATING_ELAPSED_TIME_FIELD_NUMBER: _ClassVar[int]
    DOWNLOADING_ELAPSED_TIME_FIELD_NUMBER: _ClassVar[int]
    UPDATE_APPLYING_ELAPSED_TIME_FIELD_NUMBER: _ClassVar[int]
    update_firmware_version: str
    total_files_size_uncompressed: int
    total_files_num: int
    update_start_timestamp: int
    phase: UpdatePhase
    total_download_files_num: int
    total_download_files_size: int
    downloaded_files_num: int
    downloaded_bytes: int
    downloaded_files_size: int
    downloading_errors: int
    total_remove_files_num: int
    removed_files_num: int
    processed_files_num: int
    processed_files_size: int
    total_elapsed_time: _duration_pb2.Duration
    delta_generating_elapsed_time: _duration_pb2.Duration
    downloading_elapsed_time: _duration_pb2.Duration
    update_applying_elapsed_time: _duration_pb2.Duration
    def __init__(self, update_firmware_version: _Optional[str] = ..., total_files_size_uncompressed: _Optional[int] = ..., total_files_num: _Optional[int] = ..., update_start_timestamp: _Optional[int] = ..., phase: _Optional[_Union[UpdatePhase, str]] = ..., total_download_files_num: _Optional[int] = ..., total_download_files_size: _Optional[int] = ..., downloaded_files_num: _Optional[int] = ..., downloaded_bytes: _Optional[int] = ..., downloaded_files_size: _Optional[int] = ..., downloading_errors: _Optional[int] = ..., total_remove_files_num: _Optional[int] = ..., removed_files_num: _Optional[int] = ..., processed_files_num: _Optional[int] = ..., processed_files_size: _Optional[int] = ..., total_elapsed_time: _Optional[_Union[_duration_pb2.Duration, _Mapping]] = ..., delta_generating_elapsed_time: _Optional[_Union[_duration_pb2.Duration, _Mapping]] = ..., downloading_elapsed_time: _Optional[_Union[_duration_pb2.Duration, _Mapping]] = ..., update_applying_elapsed_time: _Optional[_Union[_duration_pb2.Duration, _Mapping]] = ...) -> None: ...
