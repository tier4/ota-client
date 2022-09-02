import pytest
from concurrent.futures import ThreadPoolExecutor

from otaclient.app.update_stats import (
    OTAUpdateStatsCollector,
    RegInfProcessedStats,
    RegProcessOperation,
)


class TestOTAUpdateStatsCollector:
    WORKLOAD_COUNT = 1024
    TOTAL_SIZE = WORKLOAD_COUNT * 2
    TOTAL_FILE_NUM = WORKLOAD_COUNT

    @pytest.fixture(autouse=True)
    def update_stats_collector(self):
        _collector = OTAUpdateStatsCollector()
        _collector.set_total_regular_files(123456789)  # check init
        try:
            self._collector = _collector
            _collector.start()
            yield
        finally:
            _collector.stop()

    def workload(self, idx: int):
        """
        For odd idx, op is DOWNLOAD, for even idx, op is COPY
        For each op, elapsed_ns is 1, size is 2
        """
        op = RegProcessOperation.OP_DOWNLOAD
        if idx % 2 == 0:
            op = RegProcessOperation.OP_COPY
        self._collector.report(RegInfProcessedStats(op=op, size=2, elapsed_ns=1))

    def test_ota_update_stats_collecting(self):
        self._collector.set_total_regular_files(self.TOTAL_FILE_NUM)
        with ThreadPoolExecutor(max_workers=6) as pool:
            for idx in range(self.WORKLOAD_COUNT):
                pool.submit(
                    self.workload,
                    idx,
                )
        # wait until all reported stats are processed
        self._collector.wait_staging()

        # check result
        # half workload is copy, and other half is download
        _snapshot = self._collector.get_snapshot()
        assert _snapshot.files_processed_copy == self.TOTAL_FILE_NUM // 2
        assert _snapshot.files_processed_download == self.TOTAL_FILE_NUM // 2
        assert _snapshot.elapsed_time_copy.ToNanoseconds() == self.WORKLOAD_COUNT // 2
        assert (
            _snapshot.elapsed_time_download.ToNanoseconds() == self.WORKLOAD_COUNT // 2
        )
        assert _snapshot.file_size_processed_copy == self.TOTAL_SIZE // 2
        assert _snapshot.file_size_processed_download == self.TOTAL_SIZE // 2
