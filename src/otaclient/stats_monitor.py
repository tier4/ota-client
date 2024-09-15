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
from copy import copy
from dataclasses import asdict, dataclass
from enum import Enum, auto
from threading import Thread
from typing import Optional, Union

_otaclient_shutdown = False
_status_collector_thread: threading.Thread | None = None
_status_push_thread: threading.Thread | None = None


def _global_shutdown():
    global _otaclient_shutdown
    _otaclient_shutdown = True

    if _status_collector_thread:
        _status_collector_thread.join()
    if _status_push_thread:
        _status_push_thread.join()


atexit.register(_global_shutdown)

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
    SET_OTA_UPDATE_PHASE = "SET_OTA_UPDATE_PHASE"
    SET_OTA_STATUS = "SET_OTA_STATUS"
    SET_OTA_UPDATE_PROGRESS = "SET_OTA_UPDATE_PROGRESS"
    SET_OTA_UPDATE_META = "SET_OTA_UPDATE_META"


class FailureType(str, Enum):
    NO_FAILURE = "NO_FAILURE"
    RECOVERABLE = "RECOVERABLE"
    UNRECOVERABLE = "UNRECOVERABLE"


#
# ------ status push message type ------ #
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

    ota_status: OTAStatus = OTAStatus.INITIALIZED
    session_id: Optional[str] = None
    update_phase: Optional[UpdatePhase] = None
    update_meta: Optional[UpdateMeta] = None
    update_progress: Optional[UpdateProgress] = None
    update_timing: Optional[UpdateTiming] = None
    failure_type: Optional[FailureType] = None
    failure_reason: Optional[str] = None

    def _on_session_finished(self, payload: OTAStatusChangeReport):
        self.session_id = ""
        self.update_phase = None
        self.update_meta = None
        self.update_progress = None
        self.update_timing = None
        self.ota_status = payload.new_ota_status

        if payload.new_ota_status in [OTAStatus.FAILURE, OTAStatus.ROLLBACK_FAILURE]:
            self.failure_type = payload.failure_type
            self.failure_reason = payload.failure_reason
        else:
            self.failure_type = FailureType.NO_FAILURE
            self.failure_reason = ""

    def _on_new_ota_session(self, payload: OTAStatusChangeReport):
        self.ota_status = payload.new_ota_status
        self.update_phase = UpdatePhase.INITIALIZING
        self.update_meta = UpdateMeta()
        self.update_progress = UpdateProgress()
        self.update_timing = UpdateTiming()
        self.failure_type = FailureType.NO_FAILURE
        self.failure_reason = ""

    def _on_update_phase_changed(self, payload: OTAUpdatePhaseChangeReport):
        phase, trigger_timestamp = payload.new_update_phase, payload.trigger_timestamp

        update_timing = self.update_timing
        if update_timing is None:
            self.update_timing = update_timing = UpdateTiming()

        if phase == UpdatePhase.DOWNLOADING_OTA_FILES:
            update_timing.download_start_timestamp = trigger_timestamp
        elif phase == UpdatePhase.CALCULATING_DELTA:
            update_timing.delta_generate_start_timestamp = trigger_timestamp
        elif phase == UpdatePhase.APPLYING_UPDATE:
            update_timing.update_apply_start_timestamp = trigger_timestamp
        elif phase == UpdatePhase.INITIALIZING:
            update_timing.update_start_timestamp = trigger_timestamp

        self.update_phase = phase

    def _on_update_progress(self, payload: UpdateProgressReport):
        update_progress = self.update_progress
        if update_progress is None:
            self.update_progress = update_progress = UpdateProgress()

        op = payload.operation
        if (
            op == UpdateProgressReport.Type.PREPARE_LOCAL_COPY
            or op == UpdateProgressReport.Type.APPLY_DELTA
        ):
            update_progress.processed_files_num += payload.processed_file_num
            update_progress.processed_files_size += payload.processed_file_size
        # NOTE: downloading files number is not included in the processed_files_num
        elif op == UpdateProgressReport.Type.DOWNLOAD_REMOTE_COPY:
            update_progress.downloaded_bytes += payload.downloaded_bytes
            update_progress.downloaded_files_num += payload.processed_file_num
            update_progress.downloaded_files_size += payload.processed_file_size
            update_progress.downloading_errors += payload.errors
        elif op == UpdateProgressReport.Type.APPLY_REMOVE_DELTA:
            update_progress.removed_files_num += payload.processed_file_num

    def _on_update_meta(self, payload: UpdateMeta):
        _input = asdict(payload)
        update_meta = self.update_meta
        for k, v in _input.items():
            if v:
                setattr(update_meta, k, v)

    def load_report(self, report: StatsReport):
        # ------ on session start/end ------ #
        payload = report.payload
        if report.type == StatsReportType.SET_OTA_STATUS and isinstance(
            payload, OTAStatusChangeReport
        ):
            new_ota_status = payload.new_ota_status
            if new_ota_status in [OTAStatus.UPDATING, OTAStatus.ROLLBACKING]:
                self.session_id = report.session_id
                return self._on_new_ota_session(payload)

            self.session_id = ""  # clear session if we are not in an OTA
            return self._on_session_finished(payload)

        # ------ during OTA session ------ #
        report_session_id = report.session_id
        if report_session_id != self.session_id:
            return  # drop invalid report

        if report.type == StatsReportType.SET_OTA_UPDATE_PHASE and isinstance(
            payload, OTAUpdatePhaseChangeReport
        ):
            return self._on_update_phase_changed(payload)

        if report.type == StatsReportType.SET_OTA_UPDATE_PROGRESS and isinstance(
            payload, UpdateProgressReport
        ):
            return self._on_update_progress(payload)

        if report.type == StatsReportType.SET_OTA_UPDATE_META and isinstance(
            payload, SetUpdateMetaReport
        ):
            return self._on_update_meta(payload)


#
# ------ report message types for otaclient internal ------ #
#


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
    pass


@dataclass
class StatsReport:
    type: StatsReportType
    payload: Union[
        UpdateProgressReport,
        OTAStatusChangeReport,
        OTAUpdatePhaseChangeReport,
        SetUpdateMetaReport,
    ]
    session_id: str = ""


#
# ------ stats monitor implementation ------ #
#


class OTAClientStatsCollector:

    def __init__(
        self,
        msg_queue: queue.Queue[StatsReport],
        push_queue: queue.Queue[OTAClientStatus],
        *,
        min_collect_interval: int = 1,
        min_push_interval: int = 1,
        force_push_interval: int = 6,
    ) -> None:
        self.min_collect_interval = min_collect_interval
        self.min_push_interval = min_push_interval
        self.force_push_interval = force_push_interval

        self._input_queue = msg_queue
        self._push_queue = push_queue
        self._stats = OTAClientStatus()

        global _status_collector_thread
        _status_collector_thread = Thread(target=self._stats_collector_thread)
        _status_collector_thread.start()

        global _status_push_thread
        _status_push_thread = Thread(target=self._stats_collector_thread)
        _status_push_thread.start()

    # thread workers

    def _stats_collector_thread(self):
        while not _otaclient_shutdown:
            try:
                report = self._input_queue.get_nowait()
            except queue.Empty:
                time.sleep(self.min_collect_interval)
                continue
            self._stats.load_report(report)

    def _status_msg_push_thread(self) -> None:
        _last_time_pushed = 0
        _last_time_snapshot = copy(self._stats)

        while not _otaclient_shutdown:
            _now = int(time.time())
            _time_delta = _now - _last_time_pushed

            if _last_time_pushed == 0 or (
                _last_time_snapshot != self._stats
                and _time_delta > self.min_push_interval
            ):
                _new_copy = copy(self._stats)
                self._push_queue.put_nowait(_new_copy)
                _last_time_pushed = _now
                _last_time_snapshot = _new_copy
            elif _time_delta > self.force_push_interval:
                self._push_queue.put_nowait(_last_time_snapshot)
                _last_time_pushed = _now
            else:
                # 1. stat is not updated, but interval doesn't reach force_push_interval
                # 2. stat is updated, but interval doesn't reach min_push_interval
                time.sleep(self.min_push_interval)
