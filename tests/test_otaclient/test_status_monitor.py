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

import logging
import os
import random
import time
from queue import Queue
from typing import Generator

import pytest

from otaclient._types import FailureType, OTAStatus, UpdatePhase
from otaclient.status_monitor import (
    TERMINATE_SENTINEL,
    OTAClientStatusCollector,
    OTAStatusChangeReport,
    OTAUpdatePhaseChangeReport,
    SetOTAClientMetaReport,
    SetUpdateMetaReport,
    StatusReport,
    UpdateProgressReport,
)


class TestStatusMonitor:

    # update meta
    TOTAL_FILES_NUM = TOTAL_FILES_SIZE = 1000
    METADATA_SIZE = 20

    # update session data
    SESSION_ID_FOR_TEST = os.urandom(8).hex()
    UPDATE_VERSION_FOR_TEST = f"test_version_123_{os.urandom(8).hex()}"
    DELTA_NUM = DELTA_SIZE = 300
    DOWNLOAD_NUM = DWONLOAD_SIZE = TOTAL_DOWNLOAD_SIZE = 600
    MULTI_PATHS_FILE = MULTI_PATHS_FILE_SIZE = 100

    @pytest.fixture(autouse=True, scope="class")
    def msg_queue(self) -> Generator[Queue[StatusReport], None, None]:
        _queue = Queue()
        yield _queue

    @pytest.fixture(autouse=True, scope="class")
    def status_collector(self, msg_queue: Queue[StatusReport]):
        status_collector = OTAClientStatusCollector(msg_queue=msg_queue)
        _thread = status_collector.start()
        try:
            yield status_collector
        finally:
            msg_queue.put_nowait(TERMINATE_SENTINEL)
            _thread.join()

    def test_otaclient_start(
        self, status_collector: OTAClientStatusCollector, msg_queue: Queue[StatusReport]
    ):
        _test_failure_reason = "test_no_failure_reason"
        _test_current_version = "test_current_version"
        msg_queue.put_nowait(
            StatusReport(
                payload=OTAStatusChangeReport(
                    new_ota_status=OTAStatus.FAILURE,
                    failure_type=FailureType.RECOVERABLE,
                    failure_reason=_test_failure_reason,
                )
            )
        )
        msg_queue.put_nowait(
            StatusReport(
                payload=SetOTAClientMetaReport(
                    firmware_version=_test_current_version,
                )
            )
        )

        time.sleep(2)  # wait for reports being processed

        otaclient_status = status_collector.otaclient_status
        assert otaclient_status
        assert otaclient_status.firmware_version == _test_current_version
        assert otaclient_status.ota_status == OTAStatus.FAILURE
        assert otaclient_status.failure_type == FailureType.RECOVERABLE
        assert otaclient_status.failure_reason == _test_failure_reason

    def test_start_ota_update(
        self, status_collector: OTAClientStatusCollector, msg_queue: Queue[StatusReport]
    ):
        # ------ execution ------ #
        msg_queue.put_nowait(
            StatusReport(
                payload=OTAStatusChangeReport(
                    new_ota_status=OTAStatus.UPDATING,
                ),
                session_id=self.SESSION_ID_FOR_TEST,
            ),
        )
        msg_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.INITIALIZING,
                    trigger_timestamp=int(time.time()),
                ),
                session_id=self.SESSION_ID_FOR_TEST,
            )
        )
        msg_queue.put_nowait(
            StatusReport(
                payload=SetUpdateMetaReport(
                    update_firmware_version=self.UPDATE_VERSION_FOR_TEST,
                ),
                session_id=self.SESSION_ID_FOR_TEST,
            ),
        )

        # ------ assertion ------ #
        time.sleep(2)  # wait for reports being processed

        otaclient_status = status_collector.otaclient_status
        assert otaclient_status
        assert otaclient_status.session_id == self.SESSION_ID_FOR_TEST
        assert otaclient_status.ota_status == OTAStatus.UPDATING
        assert otaclient_status.update_phase == UpdatePhase.INITIALIZING
        assert (update_meta := otaclient_status.update_meta)
        assert update_meta.update_firmware_version == self.UPDATE_VERSION_FOR_TEST

    def test_process_metadata(
        self, status_collector: OTAClientStatusCollector, msg_queue: Queue[StatusReport]
    ) -> None:
        # ------ execution ------ #
        msg_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.PROCESSING_METADATA,
                    trigger_timestamp=int(time.time()),
                ),
                session_id=self.SESSION_ID_FOR_TEST,
            )
        )
        msg_queue.put_nowait(
            StatusReport(
                payload=SetUpdateMetaReport(
                    image_file_entries=self.TOTAL_FILES_NUM,
                    image_size_uncompressed=self.TOTAL_FILES_SIZE,
                    metadata_downloaded_bytes=self.METADATA_SIZE,
                ),
                session_id=self.SESSION_ID_FOR_TEST,
            )
        )

        # ------ assertion ------ #
        time.sleep(2)  # wait for reports being processed

        otaclient_status = status_collector.otaclient_status
        assert otaclient_status
        assert otaclient_status.session_id == self.SESSION_ID_FOR_TEST
        assert otaclient_status.update_phase == UpdatePhase.PROCESSING_METADATA
        assert (update_meta := otaclient_status.update_meta)
        assert update_meta.image_file_entries == self.TOTAL_FILES_NUM
        assert update_meta.image_size_uncompressed == self.TOTAL_FILES_SIZE
        assert (update_progress := otaclient_status.update_progress)
        assert update_progress.downloaded_bytes == self.METADATA_SIZE

    def test_filter_invalid_session_id(
        self, msg_queue: Queue[StatusReport], caplog: pytest.LogCaptureFixture
    ) -> None:
        """This test put reports with invalid session_id into the msg_queue.

        If the filter is working, all the later test methods will not fail.
        """
        _invalid_session_id = "invalid_session_id"

        # put an update meta change report
        msg_queue.put_nowait(
            StatusReport(
                payload=SetUpdateMetaReport(
                    image_file_entries=999999999999999,
                    image_size_uncompressed=0,
                    metadata_downloaded_bytes=999999999999999,
                ),
                session_id=_invalid_session_id,
            )
        )

        # put an update phase chagne report
        msg_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.INITIALIZING,
                    trigger_timestamp=32,
                ),
                session_id=_invalid_session_id,
            )
        )

        # put an update progress change report
        msg_queue.put_nowait(
            StatusReport(
                payload=UpdateProgressReport(
                    operation=UpdateProgressReport.Type.PREPARE_LOCAL_COPY,
                    processed_file_num=999999999999,
                    processed_file_size=9999999999999,
                ),
                session_id=_invalid_session_id,
            )
        )

        time.sleep(2)
        assert len(caplog.records) == 3  # three warning logs are issued
        assert all(_record.levelno == logging.WARNING for _record in caplog.records)

    def test_calculate_delta(
        self, status_collector: OTAClientStatusCollector, msg_queue: Queue[StatusReport]
    ) -> None:
        _now = int(time.time())

        # ------ execution ------ #
        msg_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.CALCULATING_DELTA,
                    trigger_timestamp=_now,
                ),
                session_id=self.SESSION_ID_FOR_TEST,
            )
        )

        for _ in range(self.DELTA_NUM):
            msg_queue.put_nowait(
                StatusReport(
                    payload=UpdateProgressReport(
                        operation=UpdateProgressReport.Type.PREPARE_LOCAL_COPY,
                        processed_file_num=1,
                        processed_file_size=1,
                    ),
                    session_id=self.SESSION_ID_FOR_TEST,
                )
            )

        # NOTE: we know what to download after delta being calculated
        msg_queue.put_nowait(
            StatusReport(
                payload=SetUpdateMetaReport(
                    total_download_files_num=self.DOWNLOAD_NUM,
                    total_download_files_size=self.DWONLOAD_SIZE,
                    total_remove_files_num=123,
                )
            )
        )

        # ------ assertion ------ #
        time.sleep(2)  # wait for reports being processed

        otaclient_status = status_collector.otaclient_status
        assert otaclient_status
        assert otaclient_status.session_id == self.SESSION_ID_FOR_TEST
        assert (
            update_timing := otaclient_status.update_timing
        ) and update_timing.delta_generate_start_timestamp == _now
        assert otaclient_status.update_phase == UpdatePhase.CALCULATING_DELTA
        assert (update_progress := otaclient_status.update_progress)
        assert update_progress.processed_files_num == self.DELTA_NUM
        assert update_progress.processed_files_size == self.DELTA_SIZE
        assert (update_meta := otaclient_status.update_meta)
        assert update_meta.total_download_files_num == self.DOWNLOAD_NUM
        assert update_meta.total_download_files_size == self.DWONLOAD_SIZE
        assert update_meta.total_remove_files_num == 123

    def test_download_ota_files(
        self, status_collector: OTAClientStatusCollector, msg_queue: Queue[StatusReport]
    ) -> None:
        _now = int(time.time())

        # ------ execution ------ #
        msg_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.DOWNLOADING_OTA_FILES,
                    trigger_timestamp=_now,
                ),
                session_id=self.SESSION_ID_FOR_TEST,
            )
        )

        _errors_count, _downloaded_bytes_sum = 0, 0
        for _ in range(self.DOWNLOAD_NUM):
            _errors = random.randint(0, 5)
            _downloaded_bytes = random.randint(1, 5)
            msg_queue.put_nowait(
                StatusReport(
                    payload=UpdateProgressReport(
                        operation=UpdateProgressReport.Type.DOWNLOAD_REMOTE_COPY,
                        processed_file_num=1,
                        processed_file_size=1,
                        downloaded_bytes=_downloaded_bytes,
                        errors=_errors,
                    ),
                    session_id=self.SESSION_ID_FOR_TEST,
                )
            )
            _errors_count += _errors
            _downloaded_bytes_sum += _downloaded_bytes

        # ------ assertion ------ #
        time.sleep(2)  # wait for reports being processed

        otaclient_status = status_collector.otaclient_status
        assert otaclient_status
        assert otaclient_status.ota_status == OTAStatus.UPDATING
        assert (
            update_timing := otaclient_status.update_timing
        ) and update_timing.download_start_timestamp == _now
        assert otaclient_status.update_phase == UpdatePhase.DOWNLOADING_OTA_FILES
        assert (update_progress := otaclient_status.update_progress)
        assert update_progress.downloading_errors == _errors_count
        assert update_progress.downloaded_files_num == self.DOWNLOAD_NUM
        assert update_progress.downloaded_files_size == self.DWONLOAD_SIZE
        assert (
            update_progress.downloaded_bytes
            == _downloaded_bytes_sum + self.METADATA_SIZE
        )

    def test_apply_update(
        self, status_collector: OTAClientStatusCollector, msg_queue: Queue[StatusReport]
    ) -> None:
        _now = int(time.time())

        # ------ execution ------ #
        msg_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.APPLYING_UPDATE,
                    trigger_timestamp=_now,
                ),
                session_id=self.SESSION_ID_FOR_TEST,
            )
        )

        for _ in range(self.MULTI_PATHS_FILE):
            msg_queue.put_nowait(
                StatusReport(
                    payload=UpdateProgressReport(
                        operation=UpdateProgressReport.Type.APPLY_DELTA,
                        processed_file_num=1,
                        processed_file_size=1,
                    ),
                    session_id=self.SESSION_ID_FOR_TEST,
                )
            )

        # ------ assertion ------ #
        time.sleep(2)  # wait for reports being processed
        otaclient_status = status_collector.otaclient_status
        assert otaclient_status
        assert otaclient_status.ota_status == OTAStatus.UPDATING
        assert otaclient_status.update_phase == UpdatePhase.APPLYING_UPDATE
        assert (
            update_timing := otaclient_status.update_timing
        ) and update_timing.update_apply_start_timestamp == _now

    def test_post_update(
        self, status_collector: OTAClientStatusCollector, msg_queue: Queue[StatusReport]
    ) -> None:
        _now = int(time.time())
        msg_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.PROCESSING_POSTUPDATE,
                    trigger_timestamp=_now,
                ),
                session_id=self.SESSION_ID_FOR_TEST,
            )
        )

        time.sleep(2)  # wait for reports being processed
        otaclient_status = status_collector.otaclient_status
        assert otaclient_status
        assert otaclient_status.ota_status == OTAStatus.UPDATING
        assert otaclient_status.update_phase == UpdatePhase.PROCESSING_POSTUPDATE
        assert (
            update_timing := otaclient_status.update_timing
        ) and update_timing.post_update_start_timestamp == _now

    def test_finalizing_update(
        self, status_collector: OTAClientStatusCollector, msg_queue: Queue[StatusReport]
    ) -> None:
        _now = int(time.time())
        msg_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.FINALIZING_UPDATE,
                    trigger_timestamp=_now,
                ),
                session_id=self.SESSION_ID_FOR_TEST,
            )
        )

        time.sleep(2)  # wait for reports being processed
        otaclient_status = status_collector.otaclient_status
        assert otaclient_status
        assert otaclient_status.ota_status == OTAStatus.UPDATING
        assert otaclient_status.update_phase == UpdatePhase.FINALIZING_UPDATE

    def test_confirm_update_progress(
        self, status_collector: OTAClientStatusCollector
    ) -> None:
        time.sleep(2)  # wait for reports being processed

        otaclient_status = status_collector.otaclient_status
        assert otaclient_status
        assert otaclient_status.ota_status == OTAStatus.UPDATING

        # confirm the OTA status store is expected
        assert (update_progress := otaclient_status.update_progress)
        assert update_progress.processed_files_num == self.TOTAL_FILES_NUM
        assert update_progress.processed_files_size == self.TOTAL_FILES_SIZE
