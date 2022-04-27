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
    TIME_TO_RESTART = 10

    def __init__(
        self,
        is_main,
        name,
        status,
        version,
        time_to_update=TIME_TO_UPDATE,
        time_to_restart=TIME_TO_RESTART,
    ):
        self._is_main = is_main
        self._name = name
        self._version = version
        self._version_to_update = None
        self._status = v2.Status()
        self._status.status = v2.StatusOta.Value(status)
        self._update_time = None
        self._time_to_update = time_to_update
        self._time_to_restart = time_to_restart

    def create(self):
        return Ecu(
            is_main=self._is_main,
            name=self._name,
            status=v2.StatusOta.Name(self._status.status),
            version=self._version,
            time_to_update=self._time_to_update,
            time_to_restart=self._time_to_restart,
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

        try:
            elapsed = time.time() - self._update_time
            progress_rate = elapsed / self._time_to_update
        except TypeError:  # when self._update_time is None
            elapsed = 0
            progress_rate = 0

        # Main ecu waits for all sub ecu sucesss, while sub ecu transitions to
        # success by itself. This code is intended to mimic that.
        # The actual ecu updates, restarts and then transitions to success.
        # In this code, after starting update and after time_to_update +
        # time_to_restart elapsed, it transitions to success.
        if self._is_main or elapsed < (self._time_to_update + self._time_to_restart):
            ecu.status.progress.CopyFrom(self._progress_rate_to_progress(progress_rate))
        else:  # sub ecu and elapsed time exceeds (time_to_update + time_to_restart).
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

            progress.elapsed_time_copy.FromSeconds(
                int(self._time_to_update * rate * 0.4)
            )
            progress.elapsed_time_link.FromSeconds(
                int(self._time_to_update * rate * 0.01)
            )
            progress.elapsed_time_download.FromSeconds(
                int(self._time_to_update * rate * 0.6)
            )
            progress.errors_download = int(rate * 0.1)
            progress.total_regular_file_size = self.TOTAL_REGULAR_FILE_SIZE
            progress.total_elapsed_time.FromSeconds(int(self._time_to_update * rate))
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

            progress.elapsed_time_copy.FromSeconds(int(self._time_to_update * 0.4))
            progress.elapsed_time_link.FromSeconds(int(self._time_to_update * 0.01))
            progress.elapsed_time_download.FromSeconds(int(self._time_to_update * 0.6))
            progress.errors_download = int(rate * 0.1)
            progress.total_regular_file_size = self.TOTAL_REGULAR_FILE_SIZE
            progress.total_elapsed_time.FromSeconds(self._time_to_update)
        return progress
