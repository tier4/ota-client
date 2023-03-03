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
"""Defined wrappers for otaclient_v2 protobuf message types."""


from __future__ import annotations
import otaclient_v2_pb2 as _v2
from copy import deepcopy
from typing import (
    Any,
    Generator as _Generator,
    Iterator as _Iterator,
    Mapping as _Mapping,
    Optional as _Optional,
    Tuple as _Tuple,
    Iterable as _Iterable,
    Union as _Union,
)
from typing_extensions import Self

from ._common import (
    calculate_slots,
    EnumWrapper,
    MessageWrapper,
    Duration,
    RepeatedCompositeContainer,
    RepeatedScalarContainer,
)


# enum


class FailureType(EnumWrapper):
    NO_FAILURE = _v2.NO_FAILURE
    RECOVERABLE = _v2.RECOVERABLE
    UNRECOVERABLE = _v2.UNRECOVERABLE

    def to_str(self) -> str:
        return f"{self.value:0>1}"


class StatusOta(EnumWrapper):
    INITIALIZED = _v2.INITIALIZED
    SUCCESS = _v2.SUCCESS
    FAILURE = _v2.FAILURE
    UPDATING = _v2.UPDATING
    ROLLBACKING = _v2.ROLLBACKING
    ROLLBACK_FAILURE = _v2.ROLLBACK_FAILURE


class StatusProgressPhase(EnumWrapper):
    INITIAL = _v2.INITIAL
    METADATA = _v2.METADATA
    DIRECTORY = _v2.DIRECTORY
    SYMLINK = _v2.SYMLINK
    REGULAR = _v2.REGULAR
    PERSISTENT = _v2.PERSISTENT
    POST_PROCESSING = _v2.POST_PROCESSING


class UpdatePhase(EnumWrapper):
    INITIALIZING = _v2.INITIALIZING
    PROCESSING_METADATA = _v2.PROCESSING_METADATA
    CALCULATING_DELTA = _v2.CALCULATING_DELTA
    DOWNLOADING_OTA_FILES = _v2.DOWNLOADING_OTA_FILES
    APPLYING_UPDATE = _v2.APPLYING_UPDATE
    PROCESSING_POSTUPDATE = _v2.PROCESSING_POSTUPDATE
    WAITING_FOR_SUBECU = _v2.WAITING_FOR_SUBECU
    REBOOTING = _v2.REBOOTING
    FINALIZING_UPDATE = _v2.FINALIZING_UPDATE


# message wrapper definitions


# rollback API


class RollbackRequestEcu(MessageWrapper[_v2.RollbackRequestEcu]):
    __slots__ = calculate_slots(_v2.RollbackRequestEcu)
    ecu_id: str

    def __init__(self, *, ecu_id: _Optional[str] = ...) -> None:
        ...


class RollbackRequest(MessageWrapper[_v2.RollbackRequest]):
    __slots__ = calculate_slots(_v2.RollbackRequest)
    ecu: RepeatedCompositeContainer[RollbackRequestEcu]

    def __init__(
        self,
        *,
        ecu: _Optional[_Iterable[RollbackRequestEcu]] = ...,
    ) -> None:
        ...

    def if_contains_ecu(self, ecu_id: str) -> bool:
        for _ecu in self.ecu:
            if _ecu.ecu_id == ecu_id:
                return True
        return False


class RollbackResponseEcu(MessageWrapper[_v2.RollbackResponseEcu]):
    __slots__ = calculate_slots(_v2.RollbackRequestEcu)
    ecu_id: str
    result: FailureType

    def __init__(
        self,
        *,
        ecu_id: _Optional[str] = ...,
        result: _Optional[_Union[FailureType, str]] = ...,
    ) -> None:
        ...


class RollbackResponse(MessageWrapper[_v2.RollbackResponse]):
    __slots__ = calculate_slots(_v2.RollbackResponse)
    ecu: RepeatedCompositeContainer[RollbackResponseEcu]

    def __init__(self, *, ecu: _Optional[_Iterable[RollbackResponseEcu]] = ...) -> None:
        ...

    def iter_ecu(
        self,
    ) -> _Generator[_Tuple[str, FailureType, RollbackResponseEcu], None, None]:
        for _ecu in self.ecu:
            yield _ecu.ecu_id, _ecu.result, _ecu

    def add_ecu(
        self, _response_ecu: _Union[RollbackResponseEcu, _v2.RollbackResponseEcu]
    ):
        if isinstance(_response_ecu, RollbackResponseEcu):
            self.ecu.append(_response_ecu)
        elif isinstance(_response_ecu, _v2.RollbackRequestEcu):
            self.ecu.append(RollbackResponseEcu.convert(_response_ecu))
        else:
            raise TypeError

    def merge_from(self, rollback_response: _Union[Self, _v2.RollbackResponse]):
        if isinstance(rollback_response, _v2.RollbackResponse):
            rollback_response = self.__class__.convert(rollback_response)
        # NOTE, TODO: duplication check is not done
        self.ecu.extend(rollback_response.ecu)


# status API


class StatusProgress(MessageWrapper[_v2.StatusProgress]):
    __slots__ = calculate_slots(_v2.StatusProgress)
    download_bytes: int
    elapsed_time_copy: Duration
    elapsed_time_download: Duration
    elapsed_time_link: Duration
    errors_download: int
    file_size_processed_copy: int
    file_size_processed_download: int
    file_size_processed_link: int
    files_processed_copy: int
    files_processed_download: int
    files_processed_link: int
    phase: StatusProgressPhase
    regular_files_processed: int
    total_elapsed_time: Duration
    total_regular_file_size: int
    total_regular_files: int

    def __init__(
        self,
        *,
        phase: _Optional[_Union[StatusProgressPhase, str]] = ...,
        total_regular_files: _Optional[int] = ...,
        regular_files_processed: _Optional[int] = ...,
        files_processed_copy: _Optional[int] = ...,
        files_processed_link: _Optional[int] = ...,
        files_processed_download: _Optional[int] = ...,
        file_size_processed_copy: _Optional[int] = ...,
        file_size_processed_link: _Optional[int] = ...,
        file_size_processed_download: _Optional[int] = ...,
        elapsed_time_copy: _Optional[Duration] = ...,
        elapsed_time_link: _Optional[Duration] = ...,
        elapsed_time_download: _Optional[Duration] = ...,
        errors_download: _Optional[int] = ...,
        total_regular_file_size: _Optional[int] = ...,
        total_elapsed_time: _Optional[Duration] = ...,
        download_bytes: _Optional[int] = ...,
    ) -> None:
        ...

    def get_snapshot(self) -> Self:
        return deepcopy(self)

    def add_elapsed_time(self, _field_name: str, _value: int):
        _field: Duration = getattr(self, _field_name)
        _field.add_nanoseconds(_value)


class Status(MessageWrapper[_v2.Status]):
    __slots__ = calculate_slots(_v2.Status)
    failure: FailureType
    failure_reason: str
    progress: StatusProgress
    status: StatusOta
    version: str

    def __init__(
        self,
        *,
        status: _Optional[_Union[StatusOta, str]] = ...,
        failure: _Optional[_Union[FailureType, str]] = ...,
        failure_reason: _Optional[str] = ...,
        version: _Optional[str] = ...,
        progress: _Optional[StatusProgress] = ...,
    ) -> None:
        ...

    def get_progress(self) -> StatusProgress:
        return self.progress

    def get_failure(self) -> _Tuple[FailureType, str]:
        return self.failure, self.failure_reason


class StatusRequest(MessageWrapper[_v2.StatusRequest]):
    __slots__ = calculate_slots(_v2.StatusRequest)


class StatusResponseEcu(MessageWrapper[_v2.StatusResponseEcu]):
    __slots__ = calculate_slots(_v2.StatusResponseEcu)
    ecu_id: str
    result: FailureType
    status: Status

    def __init__(
        self,
        *,
        ecu_id: _Optional[str] = ...,
        result: _Optional[_Union[FailureType, str]] = ...,
        status: _Optional[Status] = ...,
    ) -> None:
        ...


class StatusResponse(MessageWrapper[_v2.StatusResponse]):
    __slots__ = calculate_slots(_v2.StatusResponse)
    available_ecu_ids: RepeatedScalarContainer[str]
    ecu: RepeatedCompositeContainer[StatusResponseEcu]
    ecu_v2: RepeatedCompositeContainer[StatusResponseEcuV2]

    def __init__(
        self,
        ecu: _Optional[_Iterable[_Union[StatusResponseEcu, _Mapping]]] = ...,
        available_ecu_ids: _Optional[_Iterable[str]] = ...,
        ecu_v2: _Optional[_Iterable[_Union[StatusResponseEcuV2, _Mapping]]] = ...,
    ) -> None:
        ...

    def iter_ecu_status(
        self,
    ) -> _Generator[_Tuple[str, FailureType, Status], None, None]:
        """
        Returns:
            A _Tuple of (<ecu_id>, <failure_type>, <status>)
        """
        for _ecu in self.ecu:
            yield _ecu.ecu_id, _ecu.result, _ecu.status

    def iter_ecu_status_v2(self) -> _Iterator[StatusResponseEcuV2]:
        yield from self.ecu_v2

    def add_ecu(self, _response_ecu: Any):
        # v2
        if isinstance(_response_ecu, StatusResponseEcuV2):
            self.ecu_v2.append(_response_ecu)
            self.ecu.append(_response_ecu.convert_to_v1())  # v1 compat
        elif isinstance(_response_ecu, _v2.StatusResponseEcuV2):
            _converted = StatusResponseEcuV2.convert(_response_ecu)
            self.ecu_v2.append(_response_ecu)
            self.ecu.append(_converted.convert_to_v1())  # v1 compat
        # v1
        elif isinstance(_response_ecu, StatusResponseEcu):
            self.ecu.append(_response_ecu)
        elif isinstance(_response_ecu, _v2.StatusResponseEcu):
            self.ecu.append(StatusResponseEcu.convert(_response_ecu))
        else:
            raise TypeError

    def merge_from(self, status_resp: _Union[Self, _v2.StatusResponse]):
        if isinstance(status_resp, _v2.StatusResponse):
            status_resp = self.__class__.convert(status_resp)
        # merge ecu only, don't merge available_ecu_ids!
        # NOTE, TODO: duplication check is not done
        self.ecu.extend(status_resp.ecu)
        self.ecu_v2.extend(status_resp.ecu_v2)

    def get_ecu_status(
        self, ecu_id: str
    ) -> _Optional[_Tuple[str, FailureType, Status]]:
        """
        Returns:
            A _Tuple of (<ecu_id>, <failure_type>, <status>)
        """
        for _ecu_status in self.ecu:
            if _ecu_status.ecu_id == ecu_id:
                return _ecu_status.ecu_id, _ecu_status.result, _ecu_status.status

    def get_ecu_status_v2(self, ecu_id: str) -> _Optional[StatusResponseEcuV2]:
        for _ecu_status in self.ecu_v2:
            if _ecu_status.ecu_id == ecu_id:
                return _ecu_status


# status response format v2

# backward compatibility
V2_V1_PHASE_MAPPING = {
    UpdatePhase.INITIALIZING: StatusProgressPhase.INITIAL,
    UpdatePhase.PROCESSING_METADATA: StatusProgressPhase.METADATA,
    UpdatePhase.CALCULATING_DELTA: StatusProgressPhase.METADATA,
    UpdatePhase.DOWNLOADING_OTA_FILES: StatusProgressPhase.REGULAR,
    UpdatePhase.APPLYING_UPDATE: StatusProgressPhase.REGULAR,
    UpdatePhase.PROCESSING_POSTUPDATE: StatusProgressPhase.POST_PROCESSING,
    UpdatePhase.WAITING_FOR_SUBECU: StatusProgressPhase.POST_PROCESSING,
    UpdatePhase.REBOOTING: StatusProgressPhase.POST_PROCESSING,
    UpdatePhase.FINALIZING_UPDATE: StatusProgressPhase.POST_PROCESSING,
}


class UpdateStatus(MessageWrapper[_v2.UpdateStatus]):
    __slots__ = calculate_slots(_v2.UpdateStatus)
    delta_generating_elapsed_time: Duration
    downloaded_bytes: int
    downloaded_files_num: int
    downloaded_files_size: int
    downloading_elapsed_time: Duration
    downloading_errors: int
    phase: UpdatePhase
    processed_files_num: int
    processed_files_size: int
    removed_files_num: int
    total_download_files_num: int
    total_download_files_size: int
    total_elapsed_time: Duration
    total_files_num: int
    total_image_size: int
    total_remove_files_num: int
    update_applying_elapsed_time: Duration
    update_firmware_version: str
    update_start_timestamp: int

    def __init__(
        self,
        update_firmware_version: _Optional[str] = ...,
        total_image_size: _Optional[int] = ...,
        total_files_num: _Optional[int] = ...,
        update_start_timestamp: _Optional[int] = ...,
        phase: _Optional[_Union[UpdatePhase, str]] = ...,
        total_download_files_num: _Optional[int] = ...,
        total_download_files_size: _Optional[int] = ...,
        downloaded_files_num: _Optional[int] = ...,
        downloaded_bytes: _Optional[int] = ...,
        downloaded_files_size: _Optional[int] = ...,
        downloading_errors: _Optional[int] = ...,
        total_remove_files_num: _Optional[int] = ...,
        removed_files_num: _Optional[int] = ...,
        processed_files_num: _Optional[int] = ...,
        processed_files_size: _Optional[int] = ...,
        total_elapsed_time: _Optional[_Union[Duration, _Mapping]] = ...,
        delta_generating_elapsed_time: _Optional[_Union[Duration, _Mapping]] = ...,
        downloading_elapsed_time: _Optional[_Union[Duration, _Mapping]] = ...,
        update_applying_elapsed_time: _Optional[_Union[Duration, _Mapping]] = ...,
    ) -> None:
        ...

    def get_snapshot(self) -> Self:
        return deepcopy(self)

    def convert_to_v1_StatusProgress(self) -> StatusProgress:
        _snapshot = self.get_snapshot()
        _res = StatusProgress(
            phase=V2_V1_PHASE_MAPPING[_snapshot.phase],
            total_regular_files=_snapshot.total_files_num,
            regular_files_processed=_snapshot.processed_files_num,
            total_regular_file_size=_snapshot.total_image_size,
            elapsed_time_download=_snapshot.downloading_elapsed_time,
            elapsed_time_copy=_snapshot.update_applying_elapsed_time,
            errors_download=_snapshot.downloading_errors,
            total_elapsed_time=_snapshot.total_elapsed_time,
            download_bytes=_snapshot.downloaded_bytes,
        )
        # NOTE: for agent implementation with v1 status,
        #       - total processed files size is calculated by sum(<file_size_processed_*>)
        #       - (https://github.com/tier4/FMSAutowareAdapter/blob/develop/AutowareT4beta/edge/edge-core/application/domain/ota/firmware_deployment_status.py#L258)
        #         transfer rate is calculated by dividing sum(<files_processed_*>) with elapsed update time,
        #       - (https://github.com/tier4/FMSAutowareAdapter/blob/80be3f96223db3df20eb946f32120c0295957eef/AutowareT4beta/edge/edge-core/application/domain/ota/firmware_deployment_status.py#L411)
        #         remained time is calculated by the diff between sum(<file_size_processed_*>) and <total_file_size>,
        #         and then divided by the transfer rate.
        #
        #       In v2, <processed_files_num> is corresponding to v1's sum(<files_processed_*>),
        #       <processed_files_size> is corresponding to v1's sum(<file_size_processed_*>.
        #       In v2, downloading is counted in a separated phase,
        #       <processed_files_num> and <processed_files_size> is only counted in applying_update phase,
        #       downloading statistics are not included in <processed_files_num> and <processed_files_size>.
        #
        #       So we do some hacks here, we forcely include downloading statistics to
        #       applying update phase by reserving space for downloading statistics in applying update statistics.

        # processed files num
        _res.files_processed_download = _snapshot.downloaded_files_num
        # simply round all negative to 0
        _res.files_processed_copy = max(
            0, _snapshot.processed_files_num - _snapshot.downloaded_files_num
        )

        # processed files size
        _res.file_size_processed_download = _snapshot.downloaded_files_size
        _res.file_size_processed_copy = max(
            0, self.processed_files_size - _snapshot.downloaded_files_size
        )

        return _res


class StatusResponseEcuV2(MessageWrapper[_v2.StatusResponseEcuV2]):
    __slots__ = calculate_slots(_v2.StatusResponseEcuV2)
    ecu_id: str
    failure_reason: str
    failure_traceback: str
    failure_type: FailureType
    firmware_version: str
    ota_status: StatusOta
    otaclient_version: str
    update_status: UpdateStatus

    def __init__(
        self,
        ecu_id: _Optional[str] = ...,
        firmware_version: _Optional[str] = ...,
        otaclient_version: _Optional[str] = ...,
        ota_status: _Optional[_Union[StatusOta, str]] = ...,
        failure_type: _Optional[_Union[FailureType, str]] = ...,
        failure_reason: _Optional[str] = ...,
        failure_traceback: _Optional[str] = ...,
        update_status: _Optional[_Union[UpdateStatus, _Mapping]] = ...,
    ) -> None:
        ...

    def convert_to_v1(self) -> StatusResponseEcu:
        """Convert and export as StatusResponseEcu(v1)."""

        return StatusResponseEcu(
            ecu_id=self.ecu_id,
            result=FailureType.NO_FAILURE,
            status=Status(
                failure=self.failure_type,
                failure_reason=self.failure_reason,
                status=self.ota_status,
                version=self.firmware_version,
                progress=self.update_status.convert_to_v1_StatusProgress(),
            ),
        )


# update API


class UpdateRequestEcu(MessageWrapper[_v2.UpdateRequestEcu]):
    __slots__ = calculate_slots(_v2.UpdateRequestEcu)
    cookies: str
    ecu_id: str
    url: str
    version: str

    def __init__(
        self,
        *,
        ecu_id: _Optional[str] = ...,
        version: _Optional[str] = ...,
        url: _Optional[str] = ...,
        cookies: _Optional[str] = ...,
    ) -> None:
        ...


class UpdateRequest(MessageWrapper[_v2.UpdateRequest]):
    __slots__ = calculate_slots(_v2.UpdateRequest)
    ecu: RepeatedCompositeContainer[UpdateRequestEcu]

    def __init__(self, *, ecu: _Optional[_Iterable[UpdateRequestEcu]] = ...) -> None:
        ...

    def find_update_meta(self, ecu_id: str) -> _Optional[UpdateRequestEcu]:
        for _ecu in self.ecu:
            if _ecu.ecu_id == ecu_id:
                return _ecu

    def if_contains_ecu(self, ecu_id: str) -> bool:
        for _ecu in self.ecu:
            if _ecu.ecu_id == ecu_id:
                return True
        return False

    def iter_update_meta(self) -> _Generator[UpdateRequestEcu, None, None]:
        for _ecu in self.ecu:
            yield _ecu


class UpdateResponseEcu(MessageWrapper[_v2.UpdateResponseEcu]):
    __slots__ = calculate_slots(_v2.UpdateResponseEcu)
    ecu_id: str
    result: FailureType

    def __init__(
        self,
        *,
        ecu_id: _Optional[str] = ...,
        result: _Optional[_Union[FailureType, str]] = ...,
    ) -> None:
        ...


class UpdateResponse(MessageWrapper[_v2.UpdateResponse]):
    __slots__ = calculate_slots(_v2.UpdateResponse)
    ecu: RepeatedCompositeContainer[UpdateResponseEcu]

    def __init__(self, *, ecu: _Optional[_Iterable[UpdateResponseEcu]] = ...) -> None:
        ...

    def iter_ecu(self) -> _Generator[UpdateResponseEcu, None, None]:
        for _ecu in self.ecu:
            yield _ecu

    def add_ecu(self, _response_ecu: _Union[UpdateResponseEcu, _v2.UpdateResponseEcu]):
        if isinstance(_response_ecu, UpdateResponseEcu):
            self.ecu.append(_response_ecu)
        elif isinstance(_response_ecu, _v2.UpdateResponseEcu):
            self.ecu.append(UpdateResponseEcu.convert(_response_ecu))
        else:
            raise TypeError

    def merge_from(self, update_response: _Union[Self, _v2.UpdateResponse]):
        if isinstance(update_response, _v2.UpdateResponse):
            update_response = self.__class__.convert(update_response)
        # NOTE, TODO: duplication check is not done
        self.ecu.extend(update_response.ecu)
