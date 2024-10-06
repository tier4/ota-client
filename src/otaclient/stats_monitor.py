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
"""The implementation of tracking otaclient operation stats."""


from __future__ import annotations

import atexit
import queue
import threading
import time
from dataclasses import asdict, dataclass
from enum import Enum, auto
from threading import Thread
from typing import Union

from otaclient._types import (
    FailureType,
    OTAClientStatus,
    OTAStatus,
    UpdateMeta,
    UpdatePhase,
    UpdateProgress,
    UpdateTiming,
)

_otaclient_shutdown = False
_status_collector_thread: threading.Thread | None = None


def _global_shutdown():
    global _otaclient_shutdown
    _otaclient_shutdown = True

    if _status_collector_thread:
        _status_collector_thread.join()


atexit.register(_global_shutdown)


#
# ------ report message types for otaclient internal ------ #
#


@dataclass
class SetOTAClientMetaReport:
    firmware_version: str = ""


@dataclass
class UpdateProgressReport:

    class Type(Enum):
        # NOTE: PREPARE_LOCAL, DOWNLOAD_REMOTE and APPLY_DELTA are together
        #       counted as <processed_files_*>
        PREPARE_LOCAL_COPY = auto()
        DOWNLOAD_REMOTE_COPY = auto()
        APPLY_DELTA = auto()
        # for in-place update only
        APPLY_REMOVE_DELTA = auto()

    operation: Type
    processed_file_num: int = 0
    processed_file_size: int = 0  # uncompressed processed file size

    # only used by download operation
    downloaded_bytes: int = 0
    errors: int = 0


@dataclass
class OTAStatusChangeReport:
    new_ota_status: OTAStatus
    # only used when new_ota_status is failure
    failure_type: FailureType = FailureType.NO_FAILURE
    failure_reason: str = ""


@dataclass
class OTAUpdatePhaseChangeReport:
    new_update_phase: UpdatePhase
    trigger_timestamp: int  # in second


@dataclass
class SetUpdateMetaReport(UpdateMeta):
    metadata_downloaded_bytes: int = 0


@dataclass
class StatsReport:
    payload: Union[
        SetOTAClientMetaReport,
        UpdateProgressReport,
        OTAStatusChangeReport,
        OTAUpdatePhaseChangeReport,
        SetUpdateMetaReport,
    ]
    session_id: str = ""


#
# ------ helper functions ------ #
#
def _on_session_finished(
    status_storage: OTAClientStatus, payload: OTAStatusChangeReport
):
    status_storage.session_id = ""
    status_storage.update_phase = UpdatePhase.INITIALIZING
    status_storage.update_meta = UpdateMeta()
    status_storage.update_progress = UpdateProgress()
    status_storage.update_timing = UpdateTiming()
    status_storage.ota_status = payload.new_ota_status

    if payload.new_ota_status in [OTAStatus.FAILURE, OTAStatus.ROLLBACK_FAILURE]:
        status_storage.failure_type = payload.failure_type
        status_storage.failure_reason = payload.failure_reason
    else:
        status_storage.failure_type = FailureType.NO_FAILURE
        status_storage.failure_reason = ""


def _on_new_ota_session(
    status_storage: OTAClientStatus, payload: OTAStatusChangeReport
):
    status_storage.ota_status = payload.new_ota_status
    status_storage.update_phase = UpdatePhase.INITIALIZING
    status_storage.update_meta = UpdateMeta()
    status_storage.update_progress = UpdateProgress()
    status_storage.update_timing = UpdateTiming(update_start_timestamp=int(time.time()))
    status_storage.failure_type = FailureType.NO_FAILURE
    status_storage.failure_reason = ""


def _on_update_phase_changed(
    status_storage: OTAClientStatus, payload: OTAUpdatePhaseChangeReport
):
    phase, trigger_timestamp = payload.new_update_phase, payload.trigger_timestamp
    update_timing = status_storage.update_timing

    if phase == UpdatePhase.PROCESSING_POSTUPDATE:
        update_timing.post_update_start_timestamp = trigger_timestamp
    elif phase == UpdatePhase.DOWNLOADING_OTA_FILES:
        update_timing.download_start_timestamp = trigger_timestamp
    elif phase == UpdatePhase.CALCULATING_DELTA:
        update_timing.delta_generate_start_timestamp = trigger_timestamp
    elif phase == UpdatePhase.APPLYING_UPDATE:
        update_timing.update_apply_start_timestamp = trigger_timestamp

    status_storage.update_phase = phase


def _on_update_progress(status_storage: OTAClientStatus, payload: UpdateProgressReport):
    update_progress = status_storage.update_progress
    op = payload.operation
    if (
        op == UpdateProgressReport.Type.PREPARE_LOCAL_COPY
        or op == UpdateProgressReport.Type.APPLY_DELTA
    ):
        update_progress.processed_files_num += payload.processed_file_num
        update_progress.processed_files_size += payload.processed_file_size
    elif op == UpdateProgressReport.Type.DOWNLOAD_REMOTE_COPY:
        update_progress.processed_files_num += payload.processed_file_num
        update_progress.processed_files_size += payload.processed_file_size
        update_progress.downloaded_bytes += payload.downloaded_bytes
        update_progress.downloaded_files_num += payload.processed_file_num
        update_progress.downloaded_files_size += payload.processed_file_size
        update_progress.downloading_errors += payload.errors
    elif op == UpdateProgressReport.Type.APPLY_REMOVE_DELTA:
        update_progress.removed_files_num += payload.processed_file_num


def _on_update_meta(status_storage: OTAClientStatus, payload: SetUpdateMetaReport):
    _input = asdict(payload)
    update_meta = status_storage.update_meta
    for k, v in _input.items():
        if k == "metadata_downloaded_bytes" and v:
            status_storage.update_progress.downloaded_bytes += v
            continue
        if v:
            setattr(update_meta, k, v)


#
# ------ stats monitor implementation ------ #
#


class OTAClientStatsCollector:

    def __init__(
        self,
        msg_queue: queue.Queue[StatsReport],
        *,
        min_collect_interval: int = 1,
        min_push_interval: int = 1,
    ) -> None:
        self.min_collect_interval = min_collect_interval
        self.min_push_interval = min_push_interval

        self._input_queue = msg_queue
        self._stats = None

    def load_report(self, report: StatsReport):
        if self._stats is None:
            self._stats = OTAClientStatus()
        status_storage = self._stats

        payload = report.payload
        # ------ update otaclient meta ------ #
        if isinstance(payload, SetOTAClientMetaReport):
            status_storage.firmware_version = payload.firmware_version

        # ------ on session start/end ------ #
        if isinstance(payload, OTAStatusChangeReport):
            new_ota_status = payload.new_ota_status
            if new_ota_status in [OTAStatus.UPDATING, OTAStatus.ROLLBACKING]:
                status_storage.session_id = report.session_id
                return _on_new_ota_session(status_storage, payload)

            status_storage.session_id = ""  # clear session if we are not in an OTA
            return _on_session_finished(status_storage, payload)

        # ------ during OTA session ------ #
        report_session_id = report.session_id
        if report_session_id != status_storage.session_id:
            return  # drop invalid report
        if isinstance(payload, OTAUpdatePhaseChangeReport):
            return _on_update_phase_changed(status_storage, payload)
        if isinstance(payload, UpdateProgressReport):
            return _on_update_progress(status_storage, payload)
        if isinstance(payload, SetUpdateMetaReport):
            return _on_update_meta(status_storage, payload)

    def _stats_collector_thread(self) -> None:
        """Main entry of stats monitor working thread."""
        while not _otaclient_shutdown:
            try:
                report = self._input_queue.get_nowait()
                self.load_report(report)
            except queue.Empty:
                time.sleep(self.min_collect_interval)

    # API

    def start(self) -> None:
        """Start the stats_monitor thread."""
        Thread(
            target=self._stats_collector_thread,
            daemon=True,
            name="otaclient_stats_monitor",
        ).start()

    @property
    def otaclient_status(self) -> OTAClientStatus | None:
        return self._stats
