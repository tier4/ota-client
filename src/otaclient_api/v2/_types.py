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

from abc import abstractmethod
from copy import deepcopy
from functools import cached_property
from typing import Any
from typing import Iterable as _Iterable
from typing import Iterator as _Iterator
from typing import List as _List
from typing import Mapping as _Mapping
from typing import Optional as _Optional
from typing import Protocol as _Protocol
from typing import Set as _Set
from typing import TypeVar as _TypeVar
from typing import Union as _Union

from otaclient_pb2.v2 import otaclient_v2_pb2 as pb2
from typing_extensions import Self

from otaclient_common.proto_wrapper import (
    Duration,
    EnumWrapper,
    MessageWrapper,
    RepeatedCompositeContainer,
    RepeatedScalarContainer,
    calculate_slots,
)

# protocols


class ECU(_Protocol):
    ecu_id: str


ECUType = _TypeVar("ECUType", bound=ECU)


class ECUList(_Protocol[ECUType]):
    """A type of message that contains a list of ECUType."""

    ecu: _List[ECUType]

    def add_ecu(self, ecu: ECUType):
        self.ecu.append(ecu)

    def if_contains_ecu(self, ecu_id: str) -> bool:
        return self.find_ecu(ecu_id) is not None

    def find_ecu(self, ecu_id: str) -> _Optional[ECUType]:
        for ecu in self.ecu:
            if ecu.ecu_id == ecu_id:
                return ecu
        return None

    def iter_ecu(self) -> _Iterator[ECUType]:
        yield from self.ecu


class ECUV2List(_Protocol[ECUType]):
    """A type of message that contains a list of ECUType."""

    ecu_v2: _List[ECUType]

    @abstractmethod
    def add_ecu(self, ecu: ECUType):
        """NOTE: add_ecu method should also support adding ecu_v1 inst."""

    def if_contains_ecu_v2(self, ecu_id: str) -> bool:
        return self.find_ecu_v2(ecu_id) is not None

    def find_ecu_v2(self, ecu_id: str) -> _Optional[ECUType]:
        for ecu in self.ecu_v2:
            if ecu.ecu_id == ecu_id:
                return ecu
        return None

    def iter_ecu_v2(self) -> _Iterable[ECUType]:
        yield from self.ecu_v2


class ECUStatusSummary(_Protocol):
    """Common status summary protocol for StatusResponseEcuV2."""

    @property
    @abstractmethod
    def is_in_update(self) -> bool:
        """If this ECU is in UPDATING ota_status."""

    @property
    @abstractmethod
    def is_in_client_update(self) -> bool:
        """If this ECU is in CLIENT_UPDATING ota_status."""

    @property
    @abstractmethod
    def is_failed(self) -> bool:
        """If this ECU is in FAILURE ota_status."""

    @property
    @abstractmethod
    def is_success(self) -> bool:
        """If this ECU is in SUCCESS ota_status."""

    @property
    @abstractmethod
    def requires_network(self) -> bool:
        """If this ECU is in UPDATING and requires network connection for OTA."""


# enum


class FailureType(EnumWrapper):
    NO_FAILURE = pb2.NO_FAILURE
    RECOVERABLE = pb2.RECOVERABLE
    UNRECOVERABLE = pb2.UNRECOVERABLE

    def to_str(self) -> str:
        return f"{self.value:0>1}"


class AbortFailureType(EnumWrapper):
    ABORT_NO_FAILURE = pb2.ABORT_NO_FAILURE
    ABORT_FAILURE = pb2.ABORT_FAILURE


class StatusOta(EnumWrapper):
    INITIALIZED = pb2.INITIALIZED
    SUCCESS = pb2.SUCCESS
    FAILURE = pb2.FAILURE
    UPDATING = pb2.UPDATING
    ROLLBACKING = pb2.ROLLBACKING
    ROLLBACK_FAILURE = pb2.ROLLBACK_FAILURE
    CLIENT_UPDATING = pb2.CLIENT_UPDATING
    ABORTING = pb2.ABORTING
    ABORTED = pb2.ABORTED


class UpdatePhase(EnumWrapper):
    INITIALIZING = pb2.INITIALIZING
    PROCESSING_METADATA = pb2.PROCESSING_METADATA
    CALCULATING_DELTA = pb2.CALCULATING_DELTA
    DOWNLOADING_OTA_FILES = pb2.DOWNLOADING_OTA_FILES
    APPLYING_UPDATE = pb2.APPLYING_UPDATE
    PROCESSING_POSTUPDATE = pb2.PROCESSING_POSTUPDATE
    FINALIZING_UPDATE = pb2.FINALIZING_UPDATE
    DOWNLOADING_OTA_CLIENT = pb2.DOWNLOADING_OTA_CLIENT


# message wrapper definitions


# rollback API


class RollbackRequestEcu(MessageWrapper[pb2.RollbackRequestEcu]):
    __slots__ = calculate_slots(pb2.RollbackRequestEcu)
    ecu_id: str

    def __init__(self, *, ecu_id: _Optional[str] = ...) -> None: ...


class RollbackRequest(ECUList[RollbackRequestEcu], MessageWrapper[pb2.RollbackRequest]):
    __slots__ = calculate_slots(pb2.RollbackRequest)
    ecu: RepeatedCompositeContainer[RollbackRequestEcu]
    request_id: str

    def __init__(
        self,
        *,
        ecu: _Optional[_Iterable[RollbackRequestEcu]] = ...,
        request_id: _Optional[str] = ...,
    ) -> None: ...


class RollbackResponseEcu(MessageWrapper[pb2.RollbackResponseEcu]):
    __slots__ = calculate_slots(pb2.RollbackRequestEcu)
    ecu_id: str
    result: FailureType
    message: str

    def __init__(
        self,
        *,
        ecu_id: _Optional[str] = ...,
        result: _Optional[_Union[FailureType, str]] = ...,
        message: _Optional[str] = ...,
    ) -> None: ...


class RollbackResponse(
    ECUList[RollbackResponseEcu], MessageWrapper[pb2.RollbackResponse]
):
    __slots__ = calculate_slots(pb2.RollbackResponse)
    ecu: RepeatedCompositeContainer[RollbackResponseEcu]

    def __init__(
        self, *, ecu: _Optional[_Iterable[RollbackResponseEcu]] = ...
    ) -> None: ...

    def merge_from(self, rollback_response: _Union[Self, pb2.RollbackResponse]):
        if isinstance(rollback_response, pb2.RollbackResponse):
            rollback_response = self.__class__.convert(rollback_response)
        # NOTE, TODO: duplication check is not done
        self.ecu.extend(rollback_response.ecu)


# status API


class StatusRequest(MessageWrapper[pb2.StatusRequest]):
    __slots__ = calculate_slots(pb2.StatusRequest)


# status response format v2


class UpdateStatus(MessageWrapper[pb2.UpdateStatus]):
    __slots__ = calculate_slots(pb2.UpdateStatus)
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
    total_files_size_uncompressed: int
    total_remove_files_num: int
    update_applying_elapsed_time: Duration
    update_firmware_version: str
    update_start_timestamp: int

    def __init__(
        self,
        update_firmware_version: _Optional[str] = ...,
        total_files_size_uncompressed: _Optional[int] = ...,
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
    ) -> None: ...

    def get_snapshot(self) -> Self:
        return deepcopy(self)


class StatusResponseEcuV2(ECUStatusSummary, MessageWrapper[pb2.StatusResponseEcuV2]):
    __slots__ = calculate_slots(pb2.StatusResponseEcuV2)
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
    ) -> None: ...

    @property
    def is_in_update(self) -> bool:
        return self.ota_status is StatusOta.UPDATING

    @property
    def is_in_client_update(self) -> bool:
        return self.ota_status is StatusOta.CLIENT_UPDATING

    @property
    def is_failed(self) -> bool:
        return self.ota_status is StatusOta.FAILURE

    @property
    def is_success(self) -> bool:
        return self.ota_status is StatusOta.SUCCESS

    @property
    def requires_network(self) -> bool:
        return (
            self.ota_status is StatusOta.UPDATING
            and self.update_status.phase <= UpdatePhase.DOWNLOADING_OTA_FILES
        )


class StatusResponse(
    ECUV2List[StatusResponseEcuV2],
    MessageWrapper[pb2.StatusResponse],
):
    available_ecu_ids: RepeatedScalarContainer[str]
    ecu_v2: RepeatedCompositeContainer[StatusResponseEcuV2]

    def __init__(
        self,
        available_ecu_ids: _Optional[_Iterable[str]] = ...,
        ecu_v2: _Optional[_Iterable[_Union[StatusResponseEcuV2, _Mapping]]] = ...,
    ) -> None: ...

    def add_ecu(self, _response_ecu: Any):
        if isinstance(_response_ecu, StatusResponseEcuV2):
            self.ecu_v2.append(_response_ecu)
        elif isinstance(_response_ecu, pb2.StatusResponseEcuV2):
            self.ecu_v2.append(StatusResponseEcuV2.convert(_response_ecu))
        else:
            raise TypeError

    def merge_from(self, status_resp: _Union[Self, pb2.StatusResponse]):
        if isinstance(status_resp, pb2.StatusResponse):
            status_resp = self.__class__.convert(status_resp)
        # merge ecu only, don't merge available_ecu_ids!
        # NOTE, TODO: duplication check is not done
        self.ecu_v2.extend(status_resp.ecu_v2)


# update API


class UpdateRequestEcu(MessageWrapper[pb2.UpdateRequestEcu]):
    __slots__ = calculate_slots(pb2.UpdateRequestEcu)
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
    ) -> None: ...


class UpdateRequest(ECUList[UpdateRequestEcu], MessageWrapper[pb2.UpdateRequest]):
    __slots__ = calculate_slots(pb2.UpdateRequest)
    ecu: RepeatedCompositeContainer[UpdateRequestEcu]
    request_id: str

    def __init__(
        self,
        *,
        ecu: _Optional[_Iterable[UpdateRequestEcu]] = ...,
        request_id: _Optional[str] = ...,
    ) -> None: ...


class UpdateResponseEcu(MessageWrapper[pb2.UpdateResponseEcu]):
    __slots__ = calculate_slots(pb2.UpdateResponseEcu)
    ecu_id: str
    result: FailureType
    message: str

    def __init__(
        self,
        *,
        ecu_id: _Optional[str] = ...,
        result: _Optional[_Union[FailureType, str]] = ...,
        message: _Optional[str] = ...,
    ) -> None: ...


class UpdateResponse(ECUList[UpdateResponseEcu], MessageWrapper[pb2.UpdateResponse]):
    __slots__ = calculate_slots(pb2.UpdateResponse)
    ecu: RepeatedCompositeContainer[UpdateResponseEcu]

    def __init__(
        self, *, ecu: _Optional[_Iterable[UpdateResponseEcu]] = ...
    ) -> None: ...

    @cached_property
    def ecus_acked_update(self) -> _Set[str]:
        return {
            ecu_resp.ecu_id
            for ecu_resp in self.ecu
            if ecu_resp.result is FailureType.NO_FAILURE
        }

    def merge_from(self, update_response: _Union[Self, pb2.UpdateResponse]):
        if isinstance(update_response, pb2.UpdateResponse):
            update_response = self.__class__.convert(update_response)
        # NOTE, TODO: duplication check is not done
        self.ecu.extend(update_response.ecu)


# client update API


class ClientUpdateRequestEcu(MessageWrapper[pb2.UpdateRequestEcu]):
    __slots__ = calculate_slots(pb2.UpdateRequestEcu)
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
    ) -> None: ...


class ClientUpdateRequest(
    ECUList[ClientUpdateRequestEcu], MessageWrapper[pb2.UpdateRequest]
):
    __slots__ = calculate_slots(pb2.UpdateRequest)
    ecu: RepeatedCompositeContainer[ClientUpdateRequestEcu]
    request_id: str

    def __init__(
        self,
        *,
        ecu: _Optional[_Iterable[ClientUpdateRequestEcu]] = ...,
        request_id: _Optional[str] = ...,
    ) -> None: ...


class ClientUpdateResponseEcu(MessageWrapper[pb2.UpdateResponseEcu]):
    __slots__ = calculate_slots(pb2.UpdateResponseEcu)
    ecu_id: str
    result: FailureType
    message: str

    def __init__(
        self,
        *,
        ecu_id: _Optional[str] = ...,
        result: _Optional[_Union[FailureType, str]] = ...,
        message: _Optional[str] = ...,
    ) -> None: ...


class ClientUpdateResponse(
    ECUList[ClientUpdateResponseEcu], MessageWrapper[pb2.UpdateResponse]
):
    __slots__ = calculate_slots(pb2.UpdateResponse)
    ecu: RepeatedCompositeContainer[ClientUpdateResponseEcu]

    def __init__(
        self, *, ecu: _Optional[_Iterable[ClientUpdateResponseEcu]] = ...
    ) -> None: ...

    @cached_property
    def ecus_acked_update(self) -> _Set[str]:
        return {
            ecu_resp.ecu_id
            for ecu_resp in self.ecu
            if ecu_resp.result is FailureType.NO_FAILURE
        }

    def merge_from(self, update_response: _Union[Self, pb2.UpdateResponse]):
        if isinstance(update_response, pb2.UpdateResponse):
            update_response = self.__class__.convert(update_response)
        # NOTE, TODO: duplication check is not done
        self.ecu.extend(update_response.ecu)


# abort API


class AbortRequestEcu(MessageWrapper[pb2.AbortRequestEcu]):
    __slots__ = calculate_slots(pb2.AbortRequestEcu)
    ecu_id: str

    def __init__(self, *, ecu_id: _Optional[str] = ...) -> None: ...


class AbortRequest(ECUList[AbortRequestEcu], MessageWrapper[pb2.AbortRequest]):
    __slots__ = calculate_slots(pb2.AbortRequest)
    ecu: RepeatedCompositeContainer[AbortRequestEcu]
    request_id: str

    def __init__(
        self,
        *,
        ecu: _Optional[_Iterable[AbortRequestEcu]] = ...,
        request_id: _Optional[str] = ...,
    ) -> None: ...


class AbortResponseEcu(MessageWrapper[pb2.AbortResponseEcu]):
    __slots__ = calculate_slots(pb2.AbortResponseEcu)
    ecu_id: str
    result: AbortFailureType
    message: str

    def __init__(
        self,
        *,
        ecu_id: _Optional[str] = ...,
        result: _Optional[_Union[AbortFailureType, str]] = ...,
        message: _Optional[str] = ...,
    ) -> None: ...


class AbortResponse(ECUList[AbortResponseEcu], MessageWrapper[pb2.AbortResponse]):
    __slots__ = calculate_slots(pb2.AbortResponse)
    ecu: RepeatedCompositeContainer[AbortResponseEcu]

    def __init__(
        self, *, ecu: _Optional[_Iterable[AbortResponseEcu]] = ...
    ) -> None: ...

    def merge_from(self, abort_response: _Union[Self, pb2.AbortResponse]):
        if isinstance(abort_response, pb2.AbortResponse):
            abort_response = self.__class__.convert(abort_response)
        # NOTE, TODO: duplication check is not done
        self.ecu.extend(abort_response.ecu)
