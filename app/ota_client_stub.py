import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import grpc
import otaclient_v2_pb2 as v2
from ota_client import OtaClient
from ota_client_call import OtaClientCall
from ecu_info import EcuInfo

from configs import config as cfg
import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class OtaClientStub:
    def __init__(self):
        self._ota_client = OtaClient()
        self._ecu_info = EcuInfo()
        self._ota_client_call = OtaClientCall("50051")

        # dispatch the requested operations to threadpool
        self._executor = ThreadPoolExecutor()
        # a dict to hold the future for each requests if needed
        self._future = dict()

    def __del__(self):
        self._executor.shutdown()

    async def update(self, request):
        logger.info(f"{request=}")
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
                        name=secondary,  # register the task name with sub_ecu id
                    )
                )

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

            main_ecu_update_result = {
                "ecu_id": entry.ecu_id,
                "result": v2.NO_FAILURE,
            }

        # wait for all sub ecu acknowledge ota update requests
        # TODO: hard coded timeout
        response = []
        if len(tasks):  # if we have sub ecu to update
            done, pending = await asyncio.wait(tasks, timeout=10)
            for t in pending:
                ecu_id = t.get_name()
                logger.info(f"{ecu_id=}")
                response.append(
                    {"ecu_id": ecu_id, "result": v2.RECOVERABLE}
                )
                logger.error(
                    f"sub ecu {ecu_id} doesn't respond ota update request on time"
                )

            response.append([t.result() for t in done])

        response.append(main_ecu_update_result)

        logger.info(f"{response=}")
        return response

    def rollback(self, request):
        logger.info(f"{request=}")
        response = []

        # secondary ecus
        secondary_ecus = self._ecu_info.get_secondary_ecus()
        logger.info(f"{secondary_ecus=}")
        for secondary in secondary_ecus:
            entry = OtaClientStub._find_request(request.ecu, secondary)
            if entry:
                r = self._ota_client_call.rollback(request, secondary["ip_addr"])
                response.append(r)

        # my ecu
        ecu_id = self._ecu_info.get_ecu_id()  # my ecu id
        logger.info(f"{ecu_id=}")
        entry = OtaClientStub._find_request(request.ecu, ecu_id)
        logger.info(f"{entry=}")
        if entry:
            result = self._ota_client.rollback()
            logger.info(f"{result=}")
            response.append({"ecu_id": entry.ecu_id, "result": result.value})

        logger.info(f"{response=}")
        return response

    async def status(self, request):
        response = v2.UpdateResponse()

        # secondary ecus
        tasks = []
        secondary_ecus = self._ecu_info.get_secondary_ecus()
        for secondary in secondary_ecus:
            tasks.append(
                asyncio.create_task(
                    self._ota_client_call.status(request, secondary["ip_addr"]),
                    name = secondary["ecu_id"],
                )
            )
        
        # TODO: hardcoded timeout
        done, pending = await asyncio.wait(tasks, timeout=30)
        for t in done:
            exp = t.exception()
            if exp is not None:
                # task is done without any exception
                ecu_id, result = t.get_name(), t.result()
                logger.debug(f"{ecu_id=}, {result=}")

                sub_ecu = response.ecu.add()
                sub_ecu.CopyFrom(result)
            else:
                # exception raised from the task
                logger.warning(f"{ecu_id} is UNAVAILABLE")
                if isinstance(exp, grpc.RpcError):
                    if exp.code() == grpc.StatusCode.UNAVAILABLE:
                        # request was not received.
                        logger.warning(f"{ecu_id} did not receive the request")
                    else:
                        # other grpc error
                        logger.warning(f"{ecu_id} failed with grpc error {exp}")

        for t in pending:
            # task timeout
            logger.debug(f"{ecu_id=}: timeout")
            logger.warning(f"{ecu_id} maybe UNAVAILABLE")

        # my ecu
        ecu_id = self._ecu_info.get_ecu_id()  # my ecu id
        result, status = self._ota_client.status()
        logger.debug(f"myecu: {result=},{status=}")

        # construct status response
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
