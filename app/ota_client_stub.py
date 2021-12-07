import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import otaclient_v2_pb2 as v2
from ota_client import OtaClient
from ota_client_call import OtaClientCall
from ecu_info import EcuInfo
from ota_error import OtaErrorRecoverable

from configs import config as cfg
import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class OtaClientStub:
    def __init__(self):
        self._ota_client = OtaClient()
        self._ecu_info = EcuInfo()
        self._ota_client_call = OtaClientCall(cfg.SERVICE_PORT)

        # dispatch the requested operations to threadpool
        self._executor = ThreadPoolExecutor()
        # a dict to hold the future for each requests if needed
        self._future = dict()

    def __del__(self):
        self._executor.shutdown()

    def host_addr(self):
        return self._ecu_info.get_ecu_ip_addr()

    async def update(self, request):
        logger.info(f"{request=}")
        response = v2.UpdateResponse()

        # secondary ecus
        tasks = []
        secondary_ecus = self._ecu_info.get_secondary_ecus()
        logger.info(f"{secondary_ecus=}")
        # simultaneously dispatching update requests to all subecus without blocking
        for secondary in secondary_ecus:
            if OtaClientStub._find_request(request.ecu, secondary):
                tasks.append(
                    asyncio.create_task(
                        self._ota_client_call.update(request, secondary["ip_addr"]),
                        # register the task name with sub_ecu id
                        name=secondary["ecu_id"],
                    )
                )

        def can_reboot():
            st = self._secondary_ecus_status(request)
            count = 0
            for s in st:
                if s.result != v2.NO_FAILURE:
                    msg = f"Secondary ECU {s.ecu_id} failed: {s.result=}"
                    raise OtaErrorRecoverable(msg)
                if s.status.status == v2.StatusOta.FAILURE:
                    msg = f"Secondary ECU {s.ecu_id} failed: {s.status.status=}"
                    raise OtaErrorRecoverable(msg)
                if s.status.status == v2.StatusOta.SUCCESS:
                    count += 1
            if count == len(st):
                return True
            return False

        # my ecu
        ecu_id = self._ecu_info.get_ecu_id()  # my ecu id
        logger.info(f"{ecu_id=}")
        entry = OtaClientStub._find_request(request.ecu, ecu_id)
        logger.info(f"{entry=}")
        if entry:
            # we only dispatch the request, so we don't process the returned future object
            event = Event()
            self._executor.submit(
                self._ota_client.update, entry.version, entry.url, entry.cookies, event
            )
            # wait until update is initialized or error occured.
            event.wait()

            ecu = response.ecu.add()
            ecu.ecu_id = entry.ecu_id
            ecu.result = v2.NO_FAILURE

        # wait for all sub ecu acknowledge ota update requests
        # TODO: hard coded timeout
        if len(tasks):  # if we have sub ecu to update
            done, pending = await asyncio.wait(tasks, timeout=10)
            for t in pending:
                ecu = response.ecu.add()
                ecu.ecu_id = t.get_name()
                ecu.result = v2.RECOVERABLE
                logger.error(
                    f"sub ecu {ecu.ecu_id} doesn't respond ota update request on time"
                )
            for t in done:
                ecu = response.ecu.add()
                ecu.ecu_id = t.get_name()
                ecu.result = t.result()
                logger.info(f"{ecu.ecu_id=}, {ecu.result=}")

        logger.info(f"{response=}")
        return response

    def rollback(self, request):
        logger.info(f"{request=}")
        response = v2.RollbackResponse()

        # secondary ecus
        secondary_ecus = self._ecu_info.get_secondary_ecus()
        logger.info(f"{secondary_ecus=}")
        for secondary in secondary_ecus:
            entry = OtaClientStub._find_request(request.ecu, secondary)
            if entry:
                r = self._ota_client_call.rollback(request, secondary["ip_addr"])
                ecu = response.ecu.add()
                ecu.ecu_id = secondary["ecu_id"]
                ecu.result = r

        # my ecu
        ecu_id = self._ecu_info.get_ecu_id()  # my ecu id
        logger.info(f"{ecu_id=}")
        entry = OtaClientStub._find_request(request.ecu, ecu_id)
        logger.info(f"{entry=}")
        if entry:
            result = self._ota_client.rollback()
            logger.info(f"{result=}")
            ecu = response.ecu.add()
            ecu.ecu_id = ecu_id
            ecu.result = result.value

        logger.info(f"{response=}")
        return response

    def status(self, request):
        response = self._secondary_ecus_status(request)

        # my ecu
        ecu_id = self._ecu_info.get_ecu_id()  # my ecu id
        result, status = self._ota_client.status()
        logger.info(f"{result=},{status=}")
        ecu = response.ecu.add()
        ecu.ecu_id = ecu_id
        ecu.result = result.value
        ecu.status.status = v2.StatusOta.Value(status["status"])
        ecu.status.failure = v2.FailureType.Value(status["failure_type"])
        ecu.status.failure_reason = status["failure_reason"]
        ecu.status.version = status["version"]
        prg = ecu.status.progress
        dict_prg = status["update_progress"]
        # ecu.status.progress
        prg.phase = v2.StatusProgressPhase.Value(dict_prg["phase"])
        prg.total_regular_files = dict_prg["total_regular_files"]
        prg.regular_files_processed = dict_prg["regular_files_processed"]
        #
        prg.files_processed_copy = dict_prg["files_processed_copy"]
        prg.files_processed_link = dict_prg["files_processed_link"]
        prg.files_processed_download = dict_prg["files_processed_download"]
        prg.file_size_processed_copy = dict_prg["file_size_processed_copy"]
        prg.file_size_processed_link = dict_prg["file_size_processed_link"]
        prg.file_size_processed_download = dict_prg["file_size_processed_download"]
        prg.elapsed_time_copy.FromMilliseconds(dict_prg["elapsed_time_copy"])
        prg.elapsed_time_link.FromMilliseconds(dict_prg["elapsed_time_link"])
        prg.elapsed_time_download.FromMilliseconds(dict_prg["elapsed_time_download"])
        prg.errors_download = dict_prg["errors_download"]

        return response

    @staticmethod
    def _find_request(update_request, ecu_id):
        for request in update_request:
            if request.ecu_id == ecu_id:
                return request
        return None

    def _secondary_ecus_status(self, request):
        response = v2.StatusResponse()

        secondary_ecus = self._ecu_info.get_secondary_ecus()
        for secondary in secondary_ecus:
            r = self._ota_client_call.status(request, secondary["ip_addr"])
            for e in r.ecu:
                ecu = response.ecu.add()
                ecu.CopyFrom(e)

        return response
