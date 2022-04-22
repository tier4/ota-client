from pathlib import Path
import otaclient_v2_pb2 as v2

from configs import config as cfg
import log_util

from ecu import Ecu

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class OtaClientStub:
    def __init__(self, ecus: list):
        # check if all the names are unique
        names = [ecu.name for ecu in ecus]
        assert len(names) == len(set(names))
        # check if only one ecu is main
        mains = [ecu.is_main for ecu in ecus if ecu.is_main]
        assert len(mains) == 1

        self._ecus = ecus

    def host_addr(self):
        return "localhost"

    async def update(self, request: v2.UpdateRequest) -> v2.UpdateResponse:
        logger.info(f"{request=}")
        response = v2.UpdateResponse()

        for ecu in self._ecus:
            entry = OtaClientStub._find_request(request.ecu, ecu.name)
            if entry:
                logger.info(ecu)
                self._update(response, ecu.name)
                ecu.set_status("UPDATING")
                logger.info(f"{ecu.status=}")

        logger.info(f"{response=}")
        return response

    def rollback(self, request):
        logger.info(f"{request=}")
        response = v2.RollbackResponse()

        return response

    async def status(self, request: v2.StatusRequest) -> v2.StatusResponse:
        logger.info(f"{request=}")
        response = v2.StatusResponse()

        for ecu in self._ecus:
            self._status(response, ecu.name, ecu.status.value, ecu.version)

        logger.info(f"{response=}")
        return response

    @staticmethod
    def _find_request(update_request, ecu_id):
        for request in update_request:
            if request.ecu_id == ecu_id:
                return request
        return None

    def _update(self, response, ecu_id):
        ecu = response.ecu.add()
        ecu.ecu_id = ecu_id
        ecu.result = v2.FailureType.NO_FAILURE

    def _status(self, response, ecu_id, ecu_status_value, ecu_version):
        logger.info(f"{ecu_id=}, {ecu_status_value=}, {ecu_version=}")
        # TODO: when ecu is disconnected, ecu info is not generated.
        ecu = response.ecu.add()
        ecu.ecu_id = ecu_id
        ecu.result = v2.FailureType.NO_FAILURE

        ecu.status.status = ecu_status_value
        ecu.status.failure = v2.FailureType.NO_FAILURE
        ecu.status.failure_reason = ""
        ecu.status.version = ecu_version
        ecu.status.progress.phase = v2.StatusProgressPhase.REGULAR
        ecu.status.progress.total_regular_files = 99
        ecu.status.progress.regular_files_processed = 10

        ecu.status.progress.files_processed_copy = 50
        ecu.status.progress.files_processed_link = 9
        ecu.status.progress.files_processed_download = 40
        ecu.status.progress.file_size_processed_copy = 1000
        ecu.status.progress.file_size_processed_link = 100
        ecu.status.progress.file_size_processed_download = 1000

        ecu.status.progress.elapsed_time_copy.FromMilliseconds(1230)
        ecu.status.progress.elapsed_time_link.FromMilliseconds(120)
        ecu.status.progress.elapsed_time_download.FromMilliseconds(9870)
        ecu.status.progress.errors_download = 10
        ecu.status.progress.total_regular_file_size = 987654321
        ecu.status.progress.total_elapsed_time.FromMilliseconds(123456789)

        response.available_ecu_ids.extend([ecu_id])
