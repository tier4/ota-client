import asyncio
import grpc
from typing import Tuple
from functools import partial
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event, Lock, Condition

import otaclient_v2_pb2 as v2
from ota_error import OtaError, OtaErrorRecoverable, OtaErrorUnrecoverable
from ota_client import OtaClient
from ota_client_call import OtaClientCall
from ecu_info import EcuInfo

from configs import config as cfg
from configs import server_cfg
import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


def _statusprogress_msg_from_dict(input: dict) -> v2.StatusProgress:
    """
    expecting input dict to has the same structure as the statusprogress msg
    """
    from numbers import Number
    from google.protobuf.duration_pb2 import Duration

    res = v2.StatusProgress()
    for k, v in input.items():
        try:
            msg_field = getattr(res, k)
        except Exception:
            continue

        if isinstance(msg_field, Number) and isinstance(v, Number):
            setattr(res, k, v)
        elif isinstance(msg_field, Duration):
            msg_field.FromMilliseconds(v)

    return res


class OtaClientStub:
    def __init__(self):
        # dispatch the requested operations to threadpool
        self._executor = ThreadPoolExecutor()

        self._ota_client = OtaClient()
        self._ecu_info = EcuInfo()
        self._ota_client_call = OtaClientCall(server_cfg.SERVER_PORT)

        # for _get_subecu_status
        self._status_pulling_lock = Lock()
        self._cached_status_cv: Condition = Condition()
        self._cached_status: v2.StatusResponse = None
        self._cached_if_subecu_ready: bool = None

    def __del__(self):
        self._executor.shutdown()

    def host_addr(self):
        return self._ecu_info.get_ecu_ip_addr()

    async def update(self, request: v2.UpdateRequest) -> v2.UpdateResponse:
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

            loop = asyncio.get_running_loop()
            loop.run_in_executor(
                self._executor,
                partial(
                    self._update_executor,
                    entry,
                    request,
                    pre_update_event=pre_update_event,
                ),
            )

            # wait until pre-update initializing finished or error occured.
            if pre_update_event.wait(timeout=server_cfg.PRE_UPDATE_TIMEOUT):
                logger.debug("finish pre-update initializing")
                main_ecu = response.ecu.add()
                main_ecu.ecu_id = entry.ecu_id
                main_ecu.result = v2.NO_FAILURE
            else:
                logger.error(
                    f"failed to wait for ota-client finish pre-update initializing in {server_cfg.PRE_UPDATE_TIMEOUT}s"
                )
                # NOTE: not abort update even if local ota pre-update initializing failed
                main_ecu = response.ecu.add()
                main_ecu.ecu_id = entry.ecu_id
                main_ecu.result = v2.FAILURE

        # wait for all sub ecu acknowledge ota update requests
        if len(tasks):  # if we have sub ecu to update
            done, pending = await asyncio.wait(
                tasks, timeout=cfg.WAITING_SUBECU_ACK_UPDATE_REQ_TIMEOUT
            )
            for t in pending:
                ecu_id = t.get_name()
                logger.info(f"{ecu_id=}")

                sub_ecu = response.ecu.add()
                sub_ecu.ecu_id = ecu_id
                sub_ecu.result = v2.RECOVERABLE
                logger.error(
                    f"subecu {ecu_id} doesn't respond to ota update request on time"
                )
            for t in done:
                exp = t.exception()
                if exp is not None:
                    logger.error(f"connect sub ecu {ecu_id} failed: {exp!r}")
                    sub_ecu = response.ecu.add()
                    sub_ecu.ecu_id = ecu_id
                    sub_ecu.result = v2.UNRECOVERABLE  # TODO: unrecoverable?
                else:
                    for e in t.result().ecu:
                        ecu = response.ecu.add()
                        ecu.CopyFrom(e)
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
            entry = OtaClientStub._find_request(request.ecu, secondary["ecu_id"])
            if entry:
                r = self._ota_client_call.rollback(request, secondary["ip_addr"])
                for e in r.ecu:
                    ecu = response.ecu.add()
                    ecu.CopyFrom(e)

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

    async def status(self, request: v2.StatusRequest) -> v2.StatusResponse:
        response = v2.StatusResponse()

        # subecu
        # NOTE: modify the input response object in-place
        await asyncio.wait_for(
            asyncio.create_task(self._get_subecu_status(response)),
            timeout=server_cfg.WAITING_GET_SUBECU_STATUS,
        )

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
        prg.CopyFrom(_statusprogress_msg_from_dict(status["update_progress"]))
        prg.phase = v2.StatusProgressPhase.Value(status["update_progress"]["phase"])

        return response

    @staticmethod
    def _find_request(update_request, ecu_id):
        for request in update_request:
            if request.ecu_id == ecu_id:
                return request
        return None

    def _update_executor(
        self, entry, update_request: v2.UpdateRequest, *, pre_update_event: Event
    ):
        """
        entry for local ota update

        NOTE: no exceptions will be raised as there is no upper caller after the update is dispatched.
        exceptions will only be recorded by logger. Use status API to query the status of the update.
        """
        post_update_event = Event()

        # dispatch the local update to threadpool
        _future = self._executor.submit(
            self._ota_client.update,
            entry.version,
            entry.url,
            entry.cookies,
            pre_update_event=pre_update_event,
            post_update_event=post_update_event,
        )

        # FIXME:
        # If update returns "busy", it means that update was called during
        # update, so the subsequent process should not be performed.

        # pulling subECU status
        # NOTE: the method will block until all the subECUs' status are as expected
        try:
            asyncio.run(
                self._ensure_subecu_status(
                    timeout=server_cfg.WAITING_SUBECU_READY_TIMEOUT
                )
            )
            # all subECUs are updated, now the ota_client can reboot
            logger.debug("all subECUs are ready, set post_update_event")
            post_update_event.set()

            logger.debug("wait for local ota update to finish...")
            exp = _future.exception(timeout=server_cfg.LOCAL_OTA_UPDATE_TIMEOUT)
            if exp:
                raise exp
        except Exception as e:
            logger.exception("_ensure_subecu_status")
            if isinstance(e, OtaError):
                logger.error(f"ota update failed: {e!r}")
            elif isinstance(e, TimeoutError):
                logger.error(f"timeout local ota update {update_request=}")

    async def _get_subecu_status(
        self,
        response: v2.StatusResponse,
        failed_ecu: list = None,  # output
    ) -> bool:
        """
        fill the response with subecu status

        at anytime there will be only one on-going _get_subecu_status running,
        to prevent request flooding, the result will be cached


        if input failed_ecu list is not None, record the failed subECU id in it
        return true only when all subecu are reachable
        """
        secondary_ecus = self._ecu_info.get_secondary_ecus()
        if len(secondary_ecus) == 0:
            return True

        if self._status_pulling_lock.acquire(blocking=False):
            failed_ecu = [] if failed_ecu is None else failed_ecu

            # dispatch status pulling requests to all subecu
            tasks = []
            for secondary in secondary_ecus:
                request = v2.StatusRequest()
                tasks.append(
                    asyncio.create_task(
                        self._ota_client_call.status(request, secondary["ip_addr"]),
                        name=secondary["ecu_id"],
                    )
                )

            done, pending = await asyncio.wait(
                tasks, timeout=server_cfg.QUERYING_SUBECU_STATUS_TIMEOUT
            )
            for t in done:
                ecu_id = t.get_name()

                exp = t.exception()
                if exp is not None:
                    # exception raised from the task
                    failed_ecu.append(ecu_id)

                    logger.warning(f"{ecu_id} is UNAVAILABLE: {exp!r}")
                    if isinstance(exp, grpc.RpcError):
                        if exp.code() == grpc.StatusCode.UNAVAILABLE:
                            # request was not received.
                            logger.debug(f"{ecu_id} did not receive the request")
                        else:
                            # other grpc error
                            logger.debug(f"contacting {ecu_id} failed with grpc error")
                else:
                    # task is done without any exception
                    logger.debug(f"{ecu_id=} is reachable")

                    for e in t.result().ecu:
                        ecu = response.ecu.add()
                        ecu.CopyFrom(e)
                        logger.debug(f"{ecu.ecu_id=}, {ecu.result=}")

            for t in pending:
                # task timeout
                failed_ecu.append(t.get_name())
                logger.warning(f"{ecu_id=} maybe UNAVAILABLE due to connection timeout")
                # TODO: should we record these ECUs as FAILURE in the response?
                ecu = response.ecu.add()
                ecu.ecu_id = t.get_name()
                ecu.result = v2.FAILURE

            ret = True if len(pending) == 0 and len(failed_ecu) == 0 else False
            self._cached_if_subecu_ready = ret
            with self._cached_status_cv:
                self._cached_status.CopyFrom(response)
                self._cached_status_cv.notify()
            self._status_pulling_lock.release()

            return ret

        else:
            # there is an on-going pulling, use the cache or wait for the result from it
            with self._cached_status_cv:
                self._cached_status_cv.wait_for(lambda: self._cached_status)
                response.CopyFrom(self._cached_status)
            return self._cached_if_subecu_ready

    def _pulling_subecu_status(self, failed_ecu_list: list) -> Tuple[bool, bool]:
        """
        This function return two bool values:
        - all subecus are reachable (return value of _get_subecu_status)
        - all directly connected subECUs are in SUCCESS or FAILURE
        """
        response = v2.StatusResponse()
        success_ecu_list = []
        on_going_ecu_list = []

        # get the list of directly connect subecu
        subecu_directly_connected = {
            e["ecu_id"]: v2.FAILURE for e in self._ecu_info.get_secondary_ecus()
        }

        all_subecus_reachable = self._get_subecu_status(response, failed_ecu_list)

        if not all_subecus_reachable:
            return False, False

        # _get_subecu_status return True, means that all directly
        # connected subECUs are reachable
        for e in response.ecu:
            if e.result != v2.NO_FAILURE:
                failed_ecu_list.append(e.ecu_id)
                msg = f"Secondary ECU {e.ecu_id} failed: {e.result=}"
                logger.error(msg)
                continue

            # directly connected subECU
            ota_status = e.status.status
            if e.ecu_id in subecu_directly_connected:
                subecu_directly_connected[e.ecu_id] = ota_status

            if ota_status == v2.StatusOta.FAILURE:
                failed_ecu_list.append(e.ecu_id)
                logger.error(f"Secondary ECU {e.ecu_id} failed: {e.status.status=}")
            elif ota_status == v2.StatusOta.SUCCESS:
                success_ecu_list.append(e.ecu_id)
            elif ota_status == v2.StatusOta.UPDATING:
                on_going_ecu_list.append(e.ecu_id)

        logger.debug(
            "\nstatus pulling for all child ecu: \n"
            f"{failed_ecu_list=}\n"
            f"{on_going_ecu_list=}\n"
            f"{success_ecu_list=}"
        )

        # ensure directly connect ecu status
        failed_directly_connected_ecu = []

        def is_keep_pulling():
            for ecu, st in subecu_directly_connected.items():
                if st == v2.StatusOta.UPDATING:
                    return True
                elif st == v2.StatusOta.FAILURE or st == v2.FAILURE:
                    failed_directly_connected_ecu.append(ecu)
            return False

        keep_pulling = is_keep_pulling()

        if not keep_pulling:
            # all directly connected subECUs are in SUCCESS or FAILURE status
            if failed_directly_connected_ecu:
                logger.warning(
                    "all directly connected subecus have finished the ota update,"
                    "but some subECUs failed to apply the ota update."
                    f"failed directly subECUs presented: {failed_directly_connected_ecu}"
                )
                raise OtaErrorRecoverable(
                    f"failed directly subECUs presented: {failed_directly_connected_ecu}"
                )
            else:
                logger.info(
                    "all directly connected secondary ecus are updated successfully."
                )
        return True, keep_pulling

    async def _loop_pulling_subecu_status(
        self, retry: int = 6, pulling_count: int = 600
    ):
        """
        loop pulling subECUs' status recursively, until all the subECUs are in certain status
        """
        retry_count = 0

        if not self._ecu_info.get_secondary_ecus():
            return  # return when no subecu is attached

        for _ in range(pulling_count):
            # pulling interval
            await asyncio.sleep(server_cfg.LOOP_QUERYING_SUBECU_STATUS_INTERVAL)

            failed_ecu_list = []
            all_subecu_reachable, keep_pulling = self._pulling_subecu_status(
                self, failed_ecu_list
            )

            if all_subecu_reachable:
                if not keep_pulling:
                    return
            else:
                # unreachable directly connected subECUs presented
                retry_count += 1
                logger.debug(
                    f"retry pulling subECUs status due to unreachable subECUs {failed_ecu_list}, {retry_count=}"
                )

                if retry_count > retry:
                    # there is at least one subecu is unreachable, even with retrying n times
                    logger.debug(f"unreachable subECU list: {failed_ecu_list}")
                    raise OtaErrorUnrecoverable(
                        f"failed to contact subECUs: {failed_ecu_list}"
                    )

    async def _ensure_subecu_status(self, timeout: float):
        """
        loop pulling the status of subecu, return only when all subECU are in SUCCESS condition
        raise exception when timeout reach or any of the subECU is unavailable
        """

        t = asyncio.create_task(self._loop_pulling_subecu_status())
        try:
            await asyncio.wait_for(t, timeout=timeout)
        except Exception as e:
            logger.exception("_loop_pulling_subecu_status")
            if isinstance(e, asyncio.TimeoutError):
                raise OtaErrorUnrecoverable(
                    "failed to wait for all subECU to finish update on time"
                )
            else:
                # other OtaException
                raise
