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

import multiprocessing.sharedctypes as mp_sharedctypes
import multiprocessing.synchronize as mp_sync
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


class AbortState(IntEnum):
    """States for the OTA abort state machine.

    Transitions:
        NONE → REQUESTED      (Servicer: abort RPC received)
        NONE → FINAL_PHASE    (Updater: closing abort window before post_update)
        REQUESTED → ABORTING  (Updater: accepting abort between phases or at enter_final_phase)
        ABORTING → ABORTED    (Updater: cleanup done, status written to disk)
        FINAL_PHASE → NONE    (Updater: finally block on failure path)
        FINAL_PHASE → [reboot] (Updater: _finalize_update succeeds)
        ABORTED → [shutdown]  (Main: _on_shutdown)
    """

    NONE = 0
    REQUESTED = 1
    ABORTING = 2
    ABORTED = 3
    FINAL_PHASE = 4


class OTAAbortState:
    """Shared abort state machine for cross-process abort coordination."""

    def __init__(self, value: mp_sharedctypes.Synchronized):
        self._value = value

    @property
    def state(self) -> AbortState:
        """Read the current abort state."""
        return AbortState(self._value.value)

    def try_set_requested(self) -> bool:
        """Atomic compare-and-swap: NONE → REQUESTED.

        Used by Servicer when abort RPC arrives.

        Returns True if the transition succeeded (abort request recorded).
        Returns False if the state was not NONE (abort already in progress,
        or Updater already in final phase).
        """
        with self._value.get_lock():
            if self._value.value == AbortState.NONE:
                self._value.value = AbortState.REQUESTED
                return True
            return False

    def try_accept_abort(self) -> bool:
        """Atomic compare-and-swap: REQUESTED → ABORTING.

        Used by Updater at safe checkpoints between phases.

        Returns True if the transition succeeded (abort accepted).
        Returns False if no abort was requested.
        """
        with self._value.get_lock():
            if self._value.value == AbortState.REQUESTED:
                self._value.value = AbortState.ABORTING
                return True
            return False

    def set_aborted(self) -> None:
        """Atomic compare-and-swap: ABORTING → ABORTED.

        Used by Updater after abort cleanup is done.
        """
        with self._value.get_lock():
            if self._value.value == AbortState.ABORTING:
                self._value.value = AbortState.ABORTED

    def enter_final_phase(self) -> AbortState:
        """Atomic compare-and-swap: close the abort window or accept a pending abort.

        Used by Updater after _in_update() completes, before _post_update().

        - If NONE: transitions to FINAL_PHASE (abort window closed).
        - If REQUESTED: transitions to ABORTING (accept the pending abort).
        - Otherwise: no change.

        Returns the state that was found BEFORE the transition.
        """
        with self._value.get_lock():
            old = AbortState(self._value.value)
            if old == AbortState.NONE:
                self._value.value = AbortState.FINAL_PHASE
            elif old == AbortState.REQUESTED:
                self._value.value = AbortState.ABORTING
            return old

    def reset_final_phase(self) -> None:
        """Atomic compare-and-swap: FINAL_PHASE → NONE.

        Used in Updater's finally block.
        On the success path, _finalize_update() reboots and this never runs.
        On the failure path, this resets state so future abort requests work.
        """
        with self._value.get_lock():
            if self._value.value == AbortState.FINAL_PHASE:
                self._value.value = AbortState.NONE


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
