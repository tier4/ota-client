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


import logging
from concurrent.futures import ThreadPoolExecutor

import pytest

from otaclient.app.update_stats import (
    OTAUpdateStatsCollector,
    RegInfProcessedStats,
    RegProcessOperation,
)

logger = logging.getLogger(__name__)


class TestOTAUpdateStatsCollector:
    WORKLOAD_COUNT = 3000
    TOTAL_SIZE = WORKLOAD_COUNT * 2
    TOTAL_FILE_NUM = WORKLOAD_COUNT

    @pytest.fixture(autouse=True)
    def update_stats_collector(self):
        _collector = OTAUpdateStatsCollector()
        try:
            self._collector = _collector
            _collector.start()
            _collector.store.total_files_num = self.TOTAL_FILE_NUM  # check init
            _collector.store.total_files_size_uncompressed = (
                self.TOTAL_SIZE
            )  # check init
            yield
        finally:
            _collector.stop()

    def workload(self, idx: int):
        """
        idx mod 3 == 0 is DOWNLOAD,
        idx mod 3 == 1 is COPY,
        idx mod 3 == 2 is APPLY_UPDATE,

        For each op, elapsed_ns is 1, size is 2, download_bytes is 1
        """
        _remainder = idx % 3
        _report = RegInfProcessedStats(elapsed_ns=1, size=2)
        if _remainder == 0:
            _report.op = RegProcessOperation.DOWNLOAD_REMOTE_COPY
            _report.downloaded_bytes = 1
            self._collector.report_download_ota_files(_report)
        elif _remainder == 1:
            _report.op = RegProcessOperation.PREPARE_LOCAL_COPY
            self._collector.report_prepare_local_copy(_report)
        else:
            # NOTE: simulate special treatment for APPLY_DELTA operation.
            #       check update_stats.py:L116-119 for more details.
            _report.op = RegProcessOperation.APPLY_DELTA
            self._collector.report_apply_delta([_report, _report])

    def test_ota_update_stats_collecting(self):
        self._collector.store.total_files_num = self.TOTAL_FILE_NUM
        with ThreadPoolExecutor(max_workers=6) as pool:
            for idx in range(self.WORKLOAD_COUNT):
                pool.submit(
                    self.workload,
                    idx,
                )
        # wait until all reported stats are processed
        self._collector.wait_staging()

        # check result
        _snapshot = self._collector.get_snapshot()
        logger.info(f"{_snapshot=}")
        # assert static info
        assert _snapshot.total_files_num == self.TOTAL_FILE_NUM
        assert _snapshot.total_files_size_uncompressed == self.TOTAL_SIZE
        # total processed files num/size
        assert _snapshot.processed_files_num == self.TOTAL_FILE_NUM
        assert _snapshot.processed_files_size == self.TOTAL_SIZE
        # download operation
        assert _snapshot.downloaded_files_num == self.TOTAL_FILE_NUM // 3
        assert _snapshot.downloaded_files_size == self.TOTAL_SIZE // 3
        # NOTE: downloading_elapsed_time and downloaded_bytes is collected
        #       by accessing the downloader's properties, so comment out the following
        #       2 asserts.
        # assert (
        #     _snapshot.downloading_elapsed_time.export_pb().ToNanoseconds()
        #     == self.WORKLOAD_COUNT // 3
        # )
        # actual download_bytes is half of the file_size_processed_download to
        # simulate compression enabled scheme
        # assert _snapshot.downloaded_bytes == _snapshot.downloaded_files_size // 2

        # NOTE(20240703): we switch to track the delta generating elapsed time with the wall clock
        #   time instead of CPU consuming time.
        # prepare local copy operation
        # assert (
        #     _snapshot.delta_generating_elapsed_time.export_pb().ToNanoseconds()
        #     == self.WORKLOAD_COUNT // 3
        # )
        # # applying update operation
        # assert (
        #     _snapshot.update_applying_elapsed_time.export_pb().ToNanoseconds()
        #     == self.WORKLOAD_COUNT // 3
        # )
