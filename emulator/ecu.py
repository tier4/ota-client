import time
import otaclient_v2_pb2 as v2

from configs import config as cfg
import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class Ecu:
    TOTAL_REGULAR_FILES = 123456789
    TOTAL_REGULAR_FILE_SIZE = 987654321
    TIME_TO_UPDATE = 60 * 5

    def __init__(self, is_main, name, status, version, time_to_update=TIME_TO_UPDATE):
        self._is_main = is_main
        self._name = name
        self._version = version
        self._version_to_update = None
        self._status = v2.Status()
        self._status.status = v2.StatusOta.Value(status)
        self._update_time = None
        self._time_to_update_ms = time_to_update * 1000

    def reset(self):
        return Ecu(
            is_main=self._is_main,
            name=self._name,
            status=v2.StatusOta.Name(self._status.status),
            version=self._version,
            time_to_update=self._time_to_update_ms // 1000,
        )

    def change_to_success(self):
        if self._status.status != v2.StatusOta.UPDATING:
            logger.warning(f"current status: {v2.StatusOta.Name(self._status.status)}")
            return
        logger.info(f"change_to_success: {self._name=}")
        self._status.status = v2.StatusOta.SUCCESS
        self._version = self._version_to_update

    def update(self, response_ecu, version):
        ecu = response_ecu
        ecu.ecu_id = self._name
        ecu.result = v2.FailureType.NO_FAILURE

        # update status
        self._status = v2.Status()  # reset
        self._status.status = v2.StatusOta.UPDATING
        self._version_to_update = version
        self._update_time = time.time()

    def status(self, response_ecu):
        ecu = response_ecu
        ecu.ecu_id = self._name
        ecu.result = v2.FailureType.NO_FAILURE

        progress_rate = (
            0
            if self._update_time is None
            else (time.time() - self._update_time) * 1000 / self._time_to_update_ms
        )

        if self._is_main or progress_rate <= 1.2:
            ecu.status.progress.CopyFrom(self._progress_rate_to_progress(progress_rate))
        else:
            self.change_to_success()

        ecu.status.status = self._status.status
        ecu.status.failure = v2.FailureType.NO_FAILURE
        ecu.status.failure_reason = ""
        ecu.status.version = self._version

    def _progress_rate_to_progress(self, rate):
        progress = v2.StatusProgress()
        if rate == 0:
            progress.phase = v2.StatusProgressPhase.INITIAL
        elif rate <= 0.01:
            progress.phase = v2.StatusProgressPhase.METADATA
        elif rate <= 0.02:
            progress.phase = v2.StatusProgressPhase.DIRECTORY
        elif rate <= 0.03:
            progress.phase = v2.StatusProgressPhase.SYMLINK
        elif rate <= 0.95:
            progress.phase = v2.StatusProgressPhase.REGULAR
            progress.total_regular_files = self.TOTAL_REGULAR_FILES
            progress.regular_files_processed = int(self.TOTAL_REGULAR_FILES * rate)

            progress.files_processed_copy = int(progress.regular_files_processed * 0.4)
            progress.files_processed_link = int(progress.regular_files_processed * 0.01)
            progress.files_processed_download = (
                progress.regular_files_processed
                - progress.files_processed_copy
                - progress.files_processed_link
            )
            size_processed = int(self.TOTAL_REGULAR_FILE_SIZE * rate)
            progress.file_size_processed_copy = int(size_processed * 0.4)
            progress.file_size_processed_link = int(size_processed * 0.01)
            progress.file_size_processed_download = (
                size_processed
                - progress.file_size_processed_copy
                - progress.file_size_processed_link
            )

            progress.elapsed_time_copy.FromMilliseconds(
                int(self._time_to_update_ms * rate * 0.4)
            )
            progress.elapsed_time_link.FromMilliseconds(
                int(self._time_to_update_ms * rate * 0.01)
            )
            progress.elapsed_time_download.FromMilliseconds(
                int(self._time_to_update_ms * rate * 0.6)
            )
            progress.errors_download = int(rate * 0.1)
            progress.total_regular_file_size = self.TOTAL_REGULAR_FILE_SIZE
            progress.total_elapsed_time.FromMilliseconds(
                int(self._time_to_update_ms * rate)
            )
        else:
            progress.phase = v2.StatusProgressPhase.PERSISTENT
            progress.total_regular_files = self.TOTAL_REGULAR_FILES
            progress.regular_files_processed = self.TOTAL_REGULAR_FILES

            progress.files_processed_copy = int(progress.regular_files_processed * 0.4)
            progress.files_processed_link = int(progress.regular_files_processed * 0.01)
            progress.files_processed_download = (
                progress.regular_files_processed
                - progress.files_processed_copy
                - progress.files_processed_link
            )
            size_processed = self.TOTAL_REGULAR_FILE_SIZE
            progress.file_size_processed_copy = int(size_processed * 0.4)
            progress.file_size_processed_link = int(size_processed * 0.01)
            progress.file_size_processed_download = (
                size_processed
                - progress.file_size_processed_copy
                - progress.file_size_processed_link
            )

            progress.elapsed_time_copy.FromMilliseconds(
                int(self._time_to_update_ms * 0.4)
            )
            progress.elapsed_time_link.FromMilliseconds(
                int(self._time_to_update_ms * 0.01)
            )
            progress.elapsed_time_download.FromMilliseconds(
                int(self._time_to_update_ms * 0.6)
            )
            progress.errors_download = int(rate * 0.1)
            progress.total_regular_file_size = self.TOTAL_REGULAR_FILE_SIZE
            progress.total_elapsed_time.FromMilliseconds(int(self._time_to_update_ms))
        return progress
