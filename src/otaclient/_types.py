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
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import ClassVar

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
    ABORTING = "ABORTING"
    ABORTED = "ABORTED"


class FailureType(StrEnum):
    NO_FAILURE = "NO_FAILURE"
    RECOVERABLE = "RECOVERABLE"
    UNRECOVERABLE = "UNRECOVERABLE"


#
# ------ otaclient ABORT OTA update related flags ------ #
#


class CriticalZoneFlag:
    def __init__(self, lock: mp_sync.Lock):
        self._lock = lock

    @contextmanager
    def acquire_lock_with_release(self, blocking: bool = False):
        """Acquire lock and release it when exiting the context.

        Args:
            blocking: If True, block until the lock is acquired.
                      If False, return immediately with acquired=False if lock unavailable.

        Yields:
            bool: True if lock was acquired, False otherwise.
        """
        acquired = self._lock.acquire(block=blocking)
        try:
            yield acquired
        finally:
            if acquired:
                self._lock.release()

    def acquire_lock_no_release(self, blocking: bool = False) -> bool:
        """Acquire lock without releasing."""
        return self._lock.acquire(block=blocking)


class AbortThreadLock:
    """Wrapper for threading.Lock used to prevent multiple abort threads."""

    def __init__(self):
        self._lock = threading.Lock()

    @contextmanager
    def acquire_lock_with_release(self, blocking: bool = False):
        """Acquire lock and release it when exiting the context.

        Args:
            blocking: If True, block until the lock is acquired.
                      If False, return immediately with acquired=False if lock unavailable.

        Yields:
            bool: True if lock was acquired, False otherwise.
        """
        acquired = self._lock.acquire(blocking=blocking)
        try:
            yield acquired
        finally:
            if acquired:
                self._lock.release()

    def acquire_lock_no_release(self, blocking: bool = False) -> bool:
        """Acquire lock without releasing."""
        return self._lock.acquire(blocking=blocking)

    def release_lock(self) -> None:
        """Release the lock."""
        self._lock.release()


@dataclass
class AbortOTAFlag:
    shutdown_requested: mp_sync.Event
    reject_abort: mp_sync.Event


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
    update_meta: UpdateMeta | None = None
    update_progress: UpdateProgress | None = None
    update_timing: UpdateTiming | None = None
    failure_type: FailureType = FailureType.NO_FAILURE
    failure_reason: str = ""
    failure_traceback: str = ""
    # Path to OTA status directory (set by ota_core after boot controller init)
    ota_status_dir: str = ""


@dataclass
class MultipleECUStatusFlags:
    any_child_ecu_in_update: mp_sync.Event
    any_requires_network: mp_sync.Event
    all_success: mp_sync.Event


@dataclass
class ClientUpdateControlFlags:
    """Flags for controlling the client update process."""

    notify_data_ready_event: mp_sync.Event  # for notifying the squasfhs is ready
    request_shutdown_event: mp_sync.Event  # for requesting to shut down


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
class AbortRequestV2(IPCRequest):
    """Compatible with OTA API version 2."""


@dataclass
class ClientUpdateRequestV2(IPCRequest):
    """Compatible with OTA API version 2."""

    version: str
    url_base: str
    cookies_json: str


@dataclass
class RollbackRequestV2(IPCRequest):
    """Compatible with OTA API version 2."""
