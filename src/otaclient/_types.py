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

from dataclasses import dataclass
from enum import Enum
from typing import Optional

#
# ------ enum definitions ------ #
#


class UpdatePhase(str, Enum):
    INITIALIZING = "INITIALIZING"
    PROCESSING_METADATA = "PROCESSING_METADATA"
    CALCULATING_DELTA = "CALCULATING_DELTA"
    DOWNLOADING_OTA_FILES = "DOWNLOADING_OTA_FILES"
    APPLYING_UPDATE = "APPLYING_UPDATE"
    PROCESSING_POSTUPDATE = "PROCESSING_POSTUPDATE"
    FINALIZING_UPDATE = "FINALIZING_UPDATE"


class OTAStatus(str, Enum):
    INITIALIZED = "INITIALIZED"
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    UPDATING = "UPDATING"
    ROLLBACKING = "ROLLBACKING"
    ROLLBACK_FAILURE = "ROLLBACK_FAILURE"


class StatsReportType(str, Enum):
    SET_OTACLIENT_META = "SET_OTACLIENT_META"
    SET_OTA_UPDATE_PHASE = "SET_OTA_UPDATE_PHASE"
    SET_OTA_STATUS = "SET_OTA_STATUS"
    SET_OTA_UPDATE_PROGRESS = "SET_OTA_UPDATE_PROGRESS"
    SET_OTA_UPDATE_META = "SET_OTA_UPDATE_META"


class FailureType(str, Enum):
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


@dataclass
class OTAClientStatus:
    """otaclient internal status definition."""

    ecu_id: str = ""
    firmware_version: str = ""

    ota_status: OTAStatus = OTAStatus.INITIALIZED
    session_id: Optional[str] = None
    update_phase: Optional[UpdatePhase] = None
    update_meta: Optional[UpdateMeta] = None
    update_progress: Optional[UpdateProgress] = None
    update_timing: Optional[UpdateTiming] = None
    failure_type: Optional[FailureType] = None
    failure_reason: Optional[str] = None