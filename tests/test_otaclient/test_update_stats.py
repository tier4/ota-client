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


from __future__ import annotations

import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import pytest

from otaclient.app.update_stats import (
    OperationRecord,
    OTAUpdateStatsCollector,
    ProcessOperation,
)

logger = logging.getLogger(__name__)

PREPARE_LOCAL_COPY_TASKS = 20
DOWNLOAD_REMOTE_COPY_TASKS = 20
APPLY_DELTA_TASKS = 20


class TestOTAUpdateStatsCollector:

    @pytest.fixture(autouse=True)
    def setup_test(self):
        self._collector = OTAUpdateStatsCollector()

    def _common_worker(self, *, op: ProcessOperation):
        time.sleep(random.random())
        self._collector.report_stat(
            OperationRecord(
                op=op,
                processed_file_num=1,
                processed_file_size=1,
            )
        )

    def test_collecting(self):
        _prepare_local_copy_worker = partial(
            self._common_worker, op=ProcessOperation.PREPARE_LOCAL_COPY
        )
        _download_remote_copy_worker = partial(
            self._common_worker, op=ProcessOperation.DOWNLOAD_REMOTE_COPY
        )
        _apply_update_worker = partial(
            self._common_worker, op=ProcessOperation.APPLY_DELTA
        )

        collector = self._collector
        collector.start_collector()
        logger.info("collector started")

        with ThreadPoolExecutor(max_workers=3) as pool:
            collector.delta_calculation_started()
            for _ in range(PREPARE_LOCAL_COPY_TASKS):
                pool.submit(_prepare_local_copy_worker)
        collector.delta_calculation_finished()
        logger.info("delta calculation finished")

        with ThreadPoolExecutor(max_workers=3) as pool:
            collector.download_started()
            for _ in range(DOWNLOAD_REMOTE_COPY_TASKS):
                pool.submit(_download_remote_copy_worker)
        collector.download_finished()
        logger.info("download finished")

        with ThreadPoolExecutor(max_workers=3) as pool:
            collector.apply_update_started()
            for _ in range(APPLY_DELTA_TASKS):
                pool.submit(_apply_update_worker)
        collector.apply_update_finished()
        logger.info("apply update finished")

        self._collector.shutdown_collector()

        # ------ check the result ------ #
        assert collector.total_elapsed_time
        assert collector.delta_calculation_elapsed_time
        assert collector.download_elapsed_time
        assert collector.apply_update_elapsed_time
        assert (
            collector.total_elapsed_time
            >= collector.delta_calculation_elapsed_time
            + collector.download_elapsed_time
            + collector.apply_update_elapsed_time
        )
        assert (
            collector.processed_files_num
            == PREPARE_LOCAL_COPY_TASKS + DOWNLOAD_REMOTE_COPY_TASKS + APPLY_DELTA_TASKS
        )
        assert (
            collector.processed_files_size
            == PREPARE_LOCAL_COPY_TASKS + DOWNLOAD_REMOTE_COPY_TASKS + APPLY_DELTA_TASKS
        )
        assert collector.downloaded_files_num == DOWNLOAD_REMOTE_COPY_TASKS
        assert collector.downloaded_files_size == DOWNLOAD_REMOTE_COPY_TASKS
