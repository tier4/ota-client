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
"""The implementation of tracking otaclient metrics."""

from __future__ import annotations

import os
import random
import sys
import time
from queue import Queue

import pytest

from _otaclient_version import __version__
from otaclient import metrics
from otaclient._status_monitor import (
    OTAClientStatusCollector,
    OTAStatusChangeReport,
    OTAUpdatePhaseChangeReport,
    SetUpdateMetaReport,
    StatusReport,
    UpdatePhase,
)
from otaclient._types import FailureType, OTAStatus
from otaclient.configs.cfg import ecu_info

MODULE = metrics.__name__


class TestMetricsMonitor:

    TEST_SESSION_ID_FOR_TEST = os.urandom(8).hex()

    TEST_FAILURE_TYPE = random.choice(list(FailureType))
    TEST_FAILURE_REASON = "test failure reason"
    TEST_FAILURE_TRACEBACK = "test failure traceback"

    TEST_IMAGE_FILE_ENTRIES = 10
    TEST_IMAGE_SIZE_UNCOMPRESSED = 1024
    TEST_METADATA_DOWNLOADED_BYTES = 512
    TEST_TOTAL_DOWNLOAD_FILES_NUM = 5
    TEST_TOTAL_DOWNLOAD_FILES_SIZE = 256
    TEST_TOTAL_REMOVE_FILES_NUM = 3
    TEST_TARGET_FIRMWARE_VERSION = "x.y.z"

    def test_init(self):
        ota_metrics = metrics.OTAMetrics()
        assert ota_metrics.data.otaclient_version == __version__
        assert ota_metrics.data.ecu_id == ecu_info.ecu_id

    @pytest.fixture
    def start_session(
        self, ota_status_collector: tuple[OTAClientStatusCollector, Queue[StatusReport]]
    ):
        status_collector, msg_queue = ota_status_collector
        metrics = status_collector.otaclient_metrics
        assert metrics

        # To reuse the same session id, do all the tests in one function
        # ------ execution ------ #
        msg_queue.put_nowait(
            StatusReport(
                payload=OTAStatusChangeReport(
                    new_ota_status=OTAStatus.UPDATING,
                    failure_type=self.TEST_FAILURE_TYPE,
                    failure_reason=self.TEST_FAILURE_REASON,
                    failure_traceback=self.TEST_FAILURE_TRACEBACK,
                ),
                session_id=self.TEST_SESSION_ID_FOR_TEST,
            ),
        )

        # ------ assertion ------ #
        time.sleep(2)  # wait for reports being processed
        assert metrics.data.failure_type == self.TEST_FAILURE_TYPE
        assert metrics.data.failure_reason == self.TEST_FAILURE_REASON
        assert metrics.data.failure_traceback == self.TEST_FAILURE_TRACEBACK
        assert metrics.data.failed_at_phase == OTAStatus.UPDATING

        return msg_queue, metrics

    @pytest.mark.parametrize(
        "_new_update_phase, _target_key",
        (
            (UpdatePhase.CALCULATING_DELTA, "delta_calculation_start_timestamp"),
            (UpdatePhase.DOWNLOADING_OTA_FILES, "download_start_timestamp"),
            (UpdatePhase.APPLYING_UPDATE, "apply_update_start_timestamp"),
            (UpdatePhase.PROCESSING_POSTUPDATE, "post_update_start_timestamp"),
        ),
    )
    def test_update_phase_change_report(
        self, start_session, _new_update_phase, _target_key
    ):
        msg_queue, metrics = start_session

        # ------ execution ------ #
        _timpestamp = random.randint(0, sys.maxsize)
        msg_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=_new_update_phase,
                    trigger_timestamp=_timpestamp,
                ),
                session_id=self.TEST_SESSION_ID_FOR_TEST,
            )
        )
        # ------ assertion ------ #
        time.sleep(2)  # wait for reports being processed
        assert getattr(metrics.data, _target_key) == _timpestamp

    def test_update_meta_report(self, start_session):
        msg_queue, metrics = start_session

        # ------ execution ------ #
        msg_queue.put_nowait(
            StatusReport(
                payload=SetUpdateMetaReport(
                    image_file_entries=self.TEST_IMAGE_FILE_ENTRIES,
                    image_size_uncompressed=self.TEST_IMAGE_SIZE_UNCOMPRESSED,
                    metadata_downloaded_bytes=self.TEST_METADATA_DOWNLOADED_BYTES,
                    total_download_files_num=self.TEST_TOTAL_DOWNLOAD_FILES_NUM,
                    total_download_files_size=self.TEST_TOTAL_DOWNLOAD_FILES_SIZE,
                    total_remove_files_num=self.TEST_TOTAL_REMOVE_FILES_NUM,
                    update_firmware_version=self.TEST_TARGET_FIRMWARE_VERSION,
                ),
                session_id=self.TEST_SESSION_ID_FOR_TEST,
            ),
        )
        # ------ assertion ------ #
        time.sleep(2)  # wait for reports being processed
        assert metrics.data.ota_image_total_files_num == self.TEST_IMAGE_FILE_ENTRIES
        assert (
            metrics.data.ota_image_total_files_size == self.TEST_IMAGE_SIZE_UNCOMPRESSED
        )
        assert metrics.data.downloaded_bytes == self.TEST_METADATA_DOWNLOADED_BYTES
        assert (
            metrics.data.delta_download_resources_num
            == self.TEST_TOTAL_DOWNLOAD_FILES_NUM
        )
        assert (
            metrics.data.delta_download_resources_size
            == self.TEST_TOTAL_DOWNLOAD_FILES_SIZE
        )
        assert (
            metrics.data.delta_remove_resources_num == self.TEST_TOTAL_REMOVE_FILES_NUM
        )
        assert metrics.data.target_firmware_version == self.TEST_TARGET_FIRMWARE_VERSION
