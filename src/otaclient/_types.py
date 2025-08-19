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
"""OTAClient internal used types."""


from __future__ import annotations

import multiprocessing.synchronize as mp_sync
from dataclasses import dataclass
from typing import ClassVar, Optional

from _otaclient_version import __version__

from otaclient.configs.cfg import ecu_info
from otaclient_common._typing import StrEnum

#
# ------ OTA status enums definitions ------ #
#


class OTAOperation(StrEnum):
    UPDATE = "UPDATE"
    ROLLBACK = "ROLLBACK"


class OTAOperationResp(StrEnum):
    ACCEPTED = "ACCEPTED"
    BUSY = "BUSY"


class UpdatePhase(StrEnum):
    INITIALIZING = "INITIALIZING"
    PROCESSING_METADATA = "PROCESSING_METADATA"
    CALCULATING_DELTA = "CALCULATING_DELTA"
    DOWNLOADING_OTA_FILES = "DOWNLOADING_OTA_FILES"
    APPLYING_UPDATE = "APPLYING_UPDATE"
    PROCESSING_POSTUPDATE = "PROCESSING_POSTUPDATE"
    FINALIZING_UPDATE = "FINALIZING_UPDATE"
    DOWNLOADING_OTA_CLIENT = "DOWNLOADING_OTA_CLIENT"


class OTAStatus(StrEnum):
    INITIALIZED = "INITIALIZED"
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    UPDATING = "UPDATING"
    ROLLBACKING = "ROLLBACKING"
    ROLLBACK_FAILURE = "ROLLBACK_FAILURE"
    CLIENT_UPDATING = "CLIENT_UPDATING"


class FailureType(StrEnum):
    NO_FAILURE = "NO_FAILURE"
    RECOVERABLE = "RECOVERABLE"
    UNRECOVERABLE = "UNRECOVERABLE"


#
# ------ otaclient internal status report ------ #
#


@dataclass
class UpdateMeta:
    update_firmware_version: str = ""
    image_size_uncompressed: int = 0
    image_file_entries: int = 0
    total_download_files_num: int = 0
    total_download_files_size: int = 0
    total_remove_files_num: int = 0


@dataclass
class UpdateProgress:
    downloaded_files_num: int = 0
    downloaded_bytes: int = 0
    downloaded_files_size: int = 0
    downloading_errors: int = 0
    removed_files_num: int = 0
    processed_files_num: int = 0
    processed_files_size: int = 0


@dataclass
class UpdateTiming:
    update_start_timestamp: int = 0  # in second
    delta_generate_start_timestamp: int = 0  # in second
    download_start_timestamp: int = 0  # in second
    update_apply_start_timestamp: int = 0  # in second
    post_update_start_timestamp: int = 0  # in second


#
# ------ otaclient internal IPC messages ------ #
#


@dataclass
class OTAClientStatus:
    """otaclient internal status definition."""

    ecu_id: ClassVar[str] = ecu_info.ecu_id
    otaclient_version: ClassVar[str] = __version__
    firmware_version: str = ""

    ota_status: OTAStatus = OTAStatus.INITIALIZED
    session_id: str = ""
    update_phase: UpdatePhase = UpdatePhase.INITIALIZING
    update_meta: Optional[UpdateMeta] = None
    update_progress: Optional[UpdateProgress] = None
    update_timing: Optional[UpdateTiming] = None
    failure_type: FailureType = FailureType.NO_FAILURE
    failure_reason: str = ""
    failure_traceback: str = ""


@dataclass
class MultipleECUStatusFlags:
    any_child_ecu_in_update: mp_sync.Event
    any_requires_network: mp_sync.Event
    all_success: mp_sync.Event


@dataclass
class ClientUpdateControlFlags:
    """Flags for controlling the client update process."""

    notify_data_ready_event: mp_sync.Event  # for notifying the squasfhs is ready
    request_shutdown_event: mp_sync.Event  # for requesting to shutdown


#
# ------ OTA requests IPC ------ #
#


class IPCResEnum(StrEnum):
    ACCEPT = "ACCEPT"
    REJECT_BUSY = "REJECT_BUSY"
    """The request has been rejected due to otaclient is busy."""
    REJECT_OTHER = "REJECT_OTHER"
    """The request has been rejected for other reason."""


@dataclass
class IPCResponse:
    res: IPCResEnum
    session_id: str
    msg: str = ""


@dataclass
class IPCRequest:
    request_id: str
    session_id: str


@dataclass
class UpdateRequestV2(IPCRequest):
    """Compatible with OTA API version 2."""

    version: str
    url_base: str
    cookies_json: str


@dataclass
class ClientUpdateRequestV2(IPCRequest):
    """Compatible with OTA API version 2."""

    version: str
    url_base: str
    cookies_json: str


@dataclass
class RollbackRequestV2(IPCRequest):
    """Compatbile with OTA API version 2."""
