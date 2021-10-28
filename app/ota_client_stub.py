import asyncio
from concurrent.futures import ThreadPoolExecutor

import otaclient_v2_pb2
from ota_client import OtaClient
from ota_client_call import OtaClientCall
from ecu_info import EcuInfo

import configs as cfg
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
            self._executor.submit(
                self._ota_client.update, entry.version, entry.url, entry.cookies
            )

            main_ecu_update_result = {
                "ecu_id": entry.ecu_id,
                "result": otaclient_v2_pb2.NO_FAILURE,
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
                    {"ecu_id": ecu_id, "result": otaclient_v2_pb2.RECOVERABLE}
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

    def status(self, request):
        response = []

        # secondary ecus
        secondary_ecus = self._ecu_info.get_secondary_ecus()
        for secondary in secondary_ecus:
            r = self._ota_client_call.status(request, secondary["ip_addr"])
            response.append(r)

        # my ecu
        ecu_id = self._ecu_info.get_ecu_id()  # my ecu id
        result, status = self._ota_client.status()
        response.append({"ecu_id": ecu_id, "result": result.value, "status": status})

        return response

    @staticmethod
    def _find_request(update_request, ecu_id):
        for request in update_request:
            if request.ecu_id == ecu_id:
                return request
        return None
