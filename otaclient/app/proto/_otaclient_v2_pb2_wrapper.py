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
"""Concrete wrapper definition for otaclient_v2_pb2 protobuf message types.

Wrapper will pretend to subclass corresponding protobuf message, and will 
have the signature of the corresponding message, along with helper methods
by MessageWrapper class. 

DO NOT call protobuf message APIs on wrapper class, because wrappers don't
actually inherite from the protobuf message type!
"""


from __future__ import annotations
import otaclient_v2_pb2 as _v2
from copy import deepcopy
from typing import (
    Generator,
    Optional,
    Tuple,
    Iterable,
    Union,
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

# NOTE: as for protoc==3.21.11, protobuf==4.21.12, protobuf Enum value is
#       plain int at runtime without subclassing anything.


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


# message wrapper definitions


# rollback API


class RollbackRequestEcu(MessageWrapper[_v2.RollbackRequestEcu]):
    __slots__ = calculate_slots(_v2.RollbackRequestEcu)
    ecu_id: str

    def __init__(self, *, ecu_id: Optional[str] = ...) -> None:
        ...


class RollbackRequest(MessageWrapper[_v2.RollbackRequest]):
    __slots__ = calculate_slots(_v2.RollbackRequest)
    ecu: RepeatedCompositeContainer[RollbackRequestEcu, _v2.RollbackRequestEcu]

    def __init__(
        self,
        *,
        ecu: Optional[Iterable[RollbackRequestEcu]] = ...,
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
        ecu_id: Optional[str] = ...,
        result: Optional[Union[FailureType, str]] = ...,
    ) -> None:
        ...


class RollbackResponse(MessageWrapper[_v2.RollbackResponse]):
    __slots__ = calculate_slots(_v2.RollbackResponse)
    ecu: RepeatedCompositeContainer[RollbackResponseEcu, _v2.RollbackResponseEcu]

    def __init__(self, *, ecu: Optional[Iterable[RollbackResponseEcu]] = ...) -> None:
        ...

    def iter_ecu(
        self,
    ) -> Generator[Tuple[str, FailureType, RollbackResponseEcu], None, None]:
        for _ecu in self.ecu:
            yield _ecu.ecu_id, _ecu.result, _ecu

    def add_ecu(
        self, _response_ecu: Union[RollbackResponseEcu, _v2.RollbackResponseEcu]
    ):
        if isinstance(_response_ecu, RollbackResponseEcu):
            self.ecu.append(_response_ecu)
        elif isinstance(_response_ecu, _v2.RollbackRequestEcu):
            self.ecu.append(RollbackResponseEcu.convert(_response_ecu))
        else:
            raise TypeError

    def merge_from(self, rollback_response: Union[Self, _v2.RollbackResponse]):
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
        phase: Optional[Union[StatusProgressPhase, str]] = ...,
        total_regular_files: Optional[int] = ...,
        regular_files_processed: Optional[int] = ...,
        files_processed_copy: Optional[int] = ...,
        files_processed_link: Optional[int] = ...,
        files_processed_download: Optional[int] = ...,
        file_size_processed_copy: Optional[int] = ...,
        file_size_processed_link: Optional[int] = ...,
        file_size_processed_download: Optional[int] = ...,
        elapsed_time_copy: Optional[Duration] = ...,
        elapsed_time_link: Optional[Duration] = ...,
        elapsed_time_download: Optional[Duration] = ...,
        errors_download: Optional[int] = ...,
        total_regular_file_size: Optional[int] = ...,
        total_elapsed_time: Optional[Duration] = ...,
        download_bytes: Optional[int] = ...,
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
        status: Optional[Union[StatusOta, str]] = ...,
        failure: Optional[Union[FailureType, str]] = ...,
        failure_reason: Optional[str] = ...,
        version: Optional[str] = ...,
        progress: Optional[StatusProgress] = ...,
    ) -> None:
        ...

    def get_progress(self) -> StatusProgress:
        return self.progress

    def get_failure(self) -> Tuple[FailureType, str]:
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
        ecu_id: Optional[str] = ...,
        result: Optional[Union[FailureType, str]] = ...,
        status: Optional[Status] = ...,
    ) -> None:
        ...


class StatusResponse(MessageWrapper[_v2.StatusResponse]):
    __slots__ = calculate_slots(_v2.StatusResponse)
    available_ecu_ids: RepeatedScalarContainer[str]
    ecu: RepeatedCompositeContainer[StatusResponseEcu, _v2.StatusResponseEcu]

    def __init__(
        self,
        *,
        available_ecu_ids: Optional[Iterable[str]] = ...,
        ecu: Optional[Iterable[StatusResponseEcu]] = ...,
    ) -> None:
        ...

    def iter_ecu_status(self) -> Generator[Tuple[str, FailureType, Status], None, None]:
        """
        Returns:
            A tuple of (<ecu_id>, <failure_type>, <status>)
        """
        for _ecu in self.ecu:
            yield _ecu.ecu_id, _ecu.result, _ecu.status

    def add_ecu(self, _response_ecu: Union[StatusResponseEcu, _v2.StatusResponseEcu]):
        if isinstance(_response_ecu, StatusResponseEcu):
            self.ecu.append(_response_ecu)
        elif isinstance(_response_ecu, _v2.StatusResponseEcu):
            self.ecu.append(StatusResponseEcu.convert(_response_ecu))
        else:
            raise TypeError

    def merge_from(self, status_resp: Union[Self, _v2.StatusResponse]):
        if isinstance(status_resp, _v2.StatusResponse):
            status_resp = self.__class__.convert(status_resp)
        # merge ecu only, don't merge available_ecu_ids!
        # NOTE, TODO: duplication check is not done
        self.ecu.extend(status_resp.ecu)

    def get_ecu_status(self, ecu_id: str) -> Optional[Tuple[str, FailureType, Status]]:
        """
        Returns:
            A tuple of (<ecu_id>, <failure_type>, <status>)
        """
        for _ecu in self.ecu:
            if _ecu.ecu_id == ecu_id:
                return _ecu.ecu_id, _ecu.result, _ecu.status


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
        ecu_id: Optional[str] = ...,
        version: Optional[str] = ...,
        url: Optional[str] = ...,
        cookies: Optional[str] = ...,
    ) -> None:
        ...


class UpdateRequest(MessageWrapper[_v2.UpdateRequest]):
    __slots__ = calculate_slots(_v2.UpdateRequest)
    ecu: RepeatedCompositeContainer[UpdateRequestEcu, _v2.UpdateRequestEcu]

    def __init__(self, *, ecu: Optional[Iterable[UpdateRequestEcu]] = ...) -> None:
        ...

    def find_update_meta(self, ecu_id: str) -> Optional[UpdateRequestEcu]:
        for _ecu in self.ecu:
            if _ecu.ecu_id == ecu_id:
                return _ecu

    def if_contains_ecu(self, ecu_id: str) -> bool:
        for _ecu in self.ecu:
            if _ecu.ecu_id == ecu_id:
                return True
        return False

    def iter_update_meta(self) -> Generator[UpdateRequestEcu, None, None]:
        for _ecu in self.ecu:
            yield _ecu


class UpdateResponseEcu(MessageWrapper[_v2.UpdateResponseEcu]):
    __slots__ = calculate_slots(_v2.UpdateResponseEcu)
    ecu_id: str
    result: FailureType

    def __init__(
        self,
        *,
        ecu_id: Optional[str] = ...,
        result: Optional[Union[FailureType, str]] = ...,
    ) -> None:
        ...


class UpdateResponse(MessageWrapper[_v2.UpdateResponse]):
    __slots__ = calculate_slots(_v2.UpdateResponse)
    ecu: RepeatedCompositeContainer[UpdateResponseEcu, _v2.UpdateResponseEcu]

    def __init__(self, *, ecu: Optional[Iterable[UpdateResponseEcu]] = ...) -> None:
        ...

    def iter_ecu(self) -> Generator[UpdateResponseEcu, None, None]:
        for _ecu in self.ecu:
            yield _ecu

    def add_ecu(self, _response_ecu: Union[UpdateResponseEcu, _v2.UpdateResponseEcu]):
        if isinstance(_response_ecu, UpdateResponseEcu):
            self.ecu.append(_response_ecu)
        elif isinstance(_response_ecu, _v2.UpdateResponseEcu):
            self.ecu.append(UpdateResponseEcu.convert(_response_ecu))
        else:
            raise TypeError

    def merge_from(self, update_response: Union[Self, _v2.UpdateResponse]):
        if isinstance(update_response, _v2.UpdateResponse):
            update_response = self.__class__.convert(update_response)
        # NOTE, TODO: duplication check is not done
        self.ecu.extend(update_response.ecu)
