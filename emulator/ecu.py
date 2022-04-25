import time

from ota_status import OtaStatus
import otaclient_v2_pb2 as v2


class Ecu:
    TOTAL_REGULAR_FILES = 123456789
    TOTAL_REGULAR_FILE_SIZE = 987654321
    TIME_TO_UPDATE_MS = 60 * 1 * 1000

    def __init__(self, is_main, name, status, version):
        self._is_main = is_main
        self._name = name
        self._version = version
        self._status = v2.Status()
        self._update_time = None

    def update(self, response):
        ecu = response.ecu.add()
        ecu.ecu_id = self._name
        ecu.result = v2.FailureType.NO_FAILURE

        # update status
        self._status = v2.Status()  # reset
        self._status.status = v2.StatusOta.UPDATING
        self._update_time = time.time()

    def status(self, response):
        ecu = response.ecu.add()
        ecu.ecu_id = self._name
        ecu.result = v2.FailureType.NO_FAILURE

        ecu.status.status = self._status.status
        ecu.status.failure = v2.FailureType.NO_FAILURE
        ecu.status.failure_reason = ""
        ecu.status.version = self._version

        progress_rate = (
            0
            if self._update_time is None
            else (time.time() - self._update_time) * 1000 / self.TIME_TO_UPDATE_MS
        )
        ecu.status.progress.CopyFrom(self._progress_rate_to_progress(progress_rate))

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
                int(self.TIME_TO_UPDATE_MS * rate * 0.4)
            )
            progress.elapsed_time_link.FromMilliseconds(
                int(self.TIME_TO_UPDATE_MS * rate * 0.01)
            )
            progress.elapsed_time_download.FromMilliseconds(
                int(self.TIME_TO_UPDATE_MS * rate * 0.6)
            )
            progress.errors_download = int(rate * 0.1)
            progress.total_regular_file_size = self.TOTAL_REGULAR_FILE_SIZE
            progress.total_elapsed_time.FromMilliseconds(
                int(self.TIME_TO_UPDATE_MS * rate)
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
                int(self.TIME_TO_UPDATE_MS * 0.4)
            )
            progress.elapsed_time_link.FromMilliseconds(
                int(self.TIME_TO_UPDATE_MS * 0.01)
            )
            progress.elapsed_time_download.FromMilliseconds(
                int(self.TIME_TO_UPDATE_MS * 0.6)
            )
            progress.errors_download = int(rate * 0.1)
            progress.total_regular_file_size = self.TOTAL_REGULAR_FILE_SIZE
            progress.total_elapsed_time.FromMilliseconds(int(self.TIME_TO_UPDATE_MS))
        return progress
