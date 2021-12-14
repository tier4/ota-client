import asyncio
import grpc
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import otaclient_v2_pb2 as v2
from ota_error import OtaErrorRecoverable, OtaErrorUnrecoverable
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
        # dispatch the requested operations to threadpool
        self._executor = ThreadPoolExecutor()

        self._ota_client = OtaClient()
        self._ecu_info = EcuInfo()
        self._ota_client_call = OtaClientCall("50051")

    def __del__(self):
        self._executor.shutdown()

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
                        name=secondary,  # register the task name with sub_ecu id
                    )
                )

        # my ecu
        ecu_id = self._ecu_info.get_ecu_id()  # my ecu id
        logger.info(f"{ecu_id=}")
        entry = OtaClientStub._find_request(request.ecu, ecu_id)
        logger.info(f"{entry=}")
        if entry:
            # dispatch update requst to ota_client only
            pre_update_event = Event()
            self._executor.submit(
                self._update_executor,
                entry,
                request,
                timeout=60,  # in seconds
                pre_update_event=pre_update_event,
            )
            # wait until pre-update initializing finished or error occured.
            pre_update_event.wait()

            main_ecu = response.ecu.add()
            main_ecu.ecu_id = entry.ecu_id
            main_ecu.result = v2.NO_FAILURE

        # wait for all sub ecu acknowledge ota update requests
        if len(tasks):  # if we have sub ecu to update
            done, pending = await asyncio.wait(tasks, timeout=60) # TODO: hard coded timeout
            for t in pending:
                ecu_id = t.get_name()
                logger.info(f"{ecu_id=}")

                sub_ecu = response.ecu.add()
                sub_ecu.ecu_id = ecu_id
                sub_ecu.result = v2.RECOVERABLE
                logger.error(
                    f"sub ecu {ecu_id} doesn't respond ota update request on time"
                )
            for t in done:
                for e in t.result().ecu:
                    ecu = response.ecu.add()
                    ecu.CopyFrom(e)
                    logger.debug(f"{ecu.ecu_id=}, {ecu.result=}")

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

    async def status(self, request: v2.StatusRequest) -> v2.StatusResponse:
        response = v2.StatusResponse()

        # subecu
        self._get_subecu_status(request, response)

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

    def _update_executor(
        self, entry, request, *, pre_update_event: Event, timeout: float = None
    ):
        post_update_event = Event()

        # dispatch the update to threadpool
        self._executor.submit(
            self._ota_client.update,
            entry.version,
            entry.url,
            entry.cookies,
            pre_update_event=pre_update_event,
            post_update_event=post_update_event,
        )

        # pulling subECU status
        # NOTE: the method will block until all the subECUs' status are as expected
        asyncio.run(self._loop_pulling_subecu_status(request, timeout=timeout))
        # all subECUs are updated, now the ota_client can reboot
        logger.debug(f"all subECUs are ready, set post_update_event")
        post_update_event.set()

    async def _get_subecu_status(
        self,
        request: v2.StatusRequest,
        response: v2.StatusResponse = None,
        failed_ecu: list = None,
    ) -> bool:
        """
        fill the input response with subecu status
        pulling only when input response is None

        if input failed_ecu list is not None, record the failed subECU id in it
        return true only when all subecu are reachable
        """
        failed_ecu = [] if failed_ecu is None else failed_ecu
        res = True

        # dispatch status pulling requests to all subecu
        tasks = []
        secondary_ecus = self._ecu_info.get_secondary_ecus()
        for secondary in secondary_ecus:
            tasks.append(
                asyncio.create_task(
                    self._ota_client_call.status(request, secondary["ip_addr"]),
                    name=secondary["ecu_id"],
                )
            )

        # TODO: hardcoded timeout
        done, pending = await asyncio.wait(tasks)
        for t in done:
            exp = t.exception()
            if exp is not None:
                # task is done without any exception
                ecu_id, result = t.get_name(), t.result()
                logger.debug(f"{ecu_id=}, {result=}")

                if response is not None:
                    sub_ecu = response.ecu.add()
                    sub_ecu.CopyFrom(result)
            else:
                # exception raised from the task
                failed_ecu.append(ecu_id)
                res = False

                logger.warning(f"{ecu_id} is UNAVAILABLE")
                if isinstance(exp, grpc.RpcError):
                    if exp.code() == grpc.StatusCode.UNAVAILABLE:
                        # request was not received.
                        logger.debug(f"{ecu_id} did not receive the request")
                    else:
                        # other grpc error
                        logger.debug(
                            f"contacting {ecu_id} failed with grpc error {exp!r}"
                        )

        res = False if len(pending) != 0 else res
        for t in pending:
            # task timeout
            failed_ecu.append(t.get_name())
            logger.debug(f"{ecu_id=}: timeout for status pulling")
            logger.warning(f"{ecu_id} maybe UNAVAILABLE due to connection timeout")

        return res

    async def _loop_pulling_subecu_status(self, request, timeout: float = None):
        """
        loop pulling the status of subecu, return only when all subECU are in SUCCESS condition
        raise exception when timeout reach or any of the subECU is unavailable
        """

        async def _pulling(target: int, retry: int = 3):
            retry_count = 0

            while True:
                st = v2.StatusResponse()
                failed_ecu_list, count = [], 0

                if self._get_subecu_status(request, st, failed_ecu_list):
                    # _get_subecu_status return True, means that all subECUs are reachable
                    for e in st.ecu:
                        if e.result != v2.NO_FAILURE:
                            msg = f"Secondary ECU {e.ecu_id} failed: {e.result=}"
                            logger.error(msg)
                            raise OtaErrorRecoverable(msg)

                        if e.status.status == v2.StatusOta.FAILURE:
                            msg = f"Secondary ECU {e.ecu_id} failed: {e.status.status=}"
                            logger.error(msg)
                            raise OtaErrorRecoverable(msg)
                        elif e.status.status == v2.StatusOta.SUCCESS:
                            count += 1

                    if count >= target:
                        logger.info("all secondary ecus are updated successfully.")
                        return
                else:
                    retry_count += 1
                    logger.debug(f"retry pulling subECUs status, {retry_count=}")

                    if retry_count > retry:
                        # there is at least one subecu is unreachable, even with retrying n times
                        logger.debug(f"failed subECU list: {failed_ecu_list}")
                        raise OtaErrorUnrecoverable(
                            f"failed to contact subECUs: {failed_ecu_list}"
                        )

        secondary_ecus = self._ecu_info.get_secondary_ecus()
        t = asyncio.create_task(
            _pulling(target=len(secondary_ecus)), name=f"subecu_status_pulling_loop"
        )
        try:
            await asyncio.wait_for(t, timeout=timeout)
        except Exception as e:
            if isinstance(e, asyncio.TimeoutError):
                raise OtaErrorUnrecoverable(
                    f"failed to wait for all subECU to finish update in time"
                )
            else:
                # other OtaException
                raise
