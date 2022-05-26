import asyncio
import grpc
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from multiprocessing import Process
from threading import Lock, Condition
from typing import Tuple

import otaclient_v2_pb2 as v2
from ota_status import OtaStatus
from ota_error import OtaErrorRecoverable
from ota_client import OtaClient, OtaStateSync
from ota_client_call import OtaClientCall
from proxy_info import proxy_cfg
from ecu_info import EcuInfo

from configs import server_cfg, config as cfg
import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


def _path_load():
    import sys
    from pathlib import Path

    project_base = Path(__file__).absolute().parent.parent
    sys.path.append(str(project_base))


_path_load()


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


class OtaProxyWrapper:
    def __init__(self):
        self._lock = Lock()
        self._closed = True
        self._server_p: Process = None
        # an event for ota_cache to signal ota_service that
        # cache scrub finished.
        self._scrub_cache_event = multiprocessing.Event()

    @staticmethod
    def _start_server(enable_cache: bool, init_cache: bool, *, scrub_cache_event):
        import uvicorn
        from ota_proxy import App

        uvicorn.run(
            App(
                cache_enabled=enable_cache,
                upper_proxy=proxy_cfg.upper_ota_proxy,
                enable_https=proxy_cfg.gateway,
                init_cache=init_cache,
                scrub_cache_event=scrub_cache_event,
            ),
            host=proxy_cfg.local_ota_proxy_listen_addr,
            port=proxy_cfg.local_ota_proxy_listen_port,
            log_level="error",
            lifespan="on",
            workers=1,
            limit_concurrency=256,
        )

    def start(self, enable_cache, init_cache) -> int:
        with self._lock:
            if self._closed:
                self._server_p = Process(
                    target=self._start_server,
                    kwargs={
                        "enable_cache": enable_cache,
                        "init_cache": init_cache,
                        "scrub_cache_event": self._scrub_cache_event,
                    },
                )
                self._server_p.start()

                self._closed = False
                logger.info(
                    f"ota proxy server started(pid={self._server_p.pid}, {enable_cache=})"
                    f"{proxy_cfg}"
                )

                return self._server_p.pid
            else:
                logger.warning("try to launch ota-proxy again")

    def wait_on_ota_cache(self, timeout: float = None):
        self._scrub_cache_event.wait(timeout=timeout)

    def stop(self, cleanup_cache=False):
        from ota_proxy.config import config as proxy_srv_cfg

        with self._lock:
            if not self._closed:
                # send SIGTERM to the server process
                self._server_p.terminate()
                self._closed = True
                logger.info("ota proxy server closed")

                if cleanup_cache:
                    import shutil

                    shutil.rmtree(proxy_srv_cfg.BASE_DIR, ignore_errors=True)


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

        # ota proxy server
        if proxy_cfg.enable_local_ota_proxy:
            self._ota_proxy = OtaProxyWrapper()

    def host_addr(self):
        return self._ecu_info.get_ecu_ip_addr()

    async def update(self, request: v2.UpdateRequest) -> v2.UpdateResponse:
        logger.info(f"{request=}")
        response = v2.UpdateResponse()

        # init state machine(P1: ota_service, P2: ota_client)
        ota_sfsm = OtaStateSync()
        ota_sfsm.start(caller=ota_sfsm._P1_ota_service)

        # start ota proxy server
        # check current ota_status, if the status is not SUCCESS,
        # always assume there is an interrupted ota update, thus reuse the cache if possible
        # NOTE: now the cache scrubing will block the ota_proxy start,
        # to ensure the proxy is fully ready before sending update requests to the subecus
        # NOTE 2: signal ota_client after ota_proxy launched
        with ota_sfsm.proceed(
            ota_sfsm._P1_ota_service, expect=ota_sfsm._START
        ) as _next:
            if proxy_cfg.enable_local_ota_proxy:
                _init_cache = self._ota_client.get_ota_status() == OtaStatus.SUCCESS
                self._ota_proxy.start(
                    enable_cache=proxy_cfg.enable_local_ota_proxy_cache,
                    init_cache=_init_cache,
                )

                # wait for ota_cache to finish initializing
                self._ota_proxy.wait_on_ota_cache()

            assert _next == ota_sfsm._S0

        # secondary ecus
        tasks = []
        secondary_ecus = self._ecu_info.get_secondary_ecus()
        logger.info(f"{secondary_ecus=}")
        # simultaneously dispatching update requests to all subecus without blocking
        for secondary in secondary_ecus:
            if OtaClientStub._find_request(request.ecu, secondary["ecu_id"]):
                tasks.append(
                    asyncio.create_task(
                        self._ota_client_call.update(request, secondary["ip_addr"]),
                        # register the task name with sub_ecu id
                        name=secondary["ecu_id"],
                    )
                )

        # my ecu
        ecu_id = self._ecu_info.get_ecu_id()  # my ecu id
        logger.info(f"{ecu_id=}")
        entry = OtaClientStub._find_request(request.ecu, ecu_id)
        logger.info(f"{entry=}")

        if entry:
            # dispatch update requst to ota_client only
            loop = asyncio.get_running_loop()
            loop.run_in_executor(
                self._executor,
                partial(
                    self._update_executor,
                    entry,
                    fsm=ota_sfsm,
                ),
            )

            # wait until pre-update initializing finished or error occured.
            if ota_sfsm.wait_on(ota_sfsm._S1, timeout=server_cfg.PRE_UPDATE_TIMEOUT):
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
        # NOTE(20220513): is it illegal that ecu itself is not requested to update
        # but its subecus do, but currently we just log errors for it
        else:
            logger.warning("entries in update request doesn't include this ecu")

        # wait for all sub ecu acknowledge ota update requests
        if len(tasks):  # if we have sub ecu to update
            done, pending = await asyncio.wait(
                tasks, timeout=server_cfg.WAITING_SUBECU_ACK_UPDATE_REQ_TIMEOUT
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
                    ecu_id = t.get_name()
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

        # available ecu ids
        available_ecu_ids = self._ecu_info.get_available_ecu_ids()
        response.available_ecu_ids.extend(available_ecu_ids)

        return response

    @staticmethod
    def _find_request(update_request, ecu_id):
        for request in update_request:
            if request.ecu_id == ecu_id:
                return request
        return None

    def _update_executor(self, entry, *, fsm: OtaStateSync):
        """
        entry for local ota update

        NOTE: no exceptions will be raised as there is no upper caller after the update is dispatched.
        exceptions will only be recorded by logger. Use status API to query the status of the update.
        """
        # dispatch the local update to threadpool
        self._executor.submit(
            self._ota_client.update, entry.version, entry.url, entry.cookies, fsm=fsm
        )

        # FIXME:
        # If update returns "busy", it means that update was called during
        # update, so the subsequent process should not be performed.

        # wait for local ota update and all subecus to finish update, and then do cleanup
        # NOTE: the failure of ota_client side update will not reflect immediately here,
        # however it is not a problem as we will track the update state via status API.
        with fsm.proceed(fsm._P1_ota_service, expect=fsm._S2) as _next:
            # ensure all subecus state
            asyncio.run(self._ensure_subecu_status())
            logger.info("all subECUs are updated and become ready")

            if proxy_cfg.enable_local_ota_proxy:
                # NOTE: the following lines can only be reached when the whole update
                # (including local update and all subecus update) are successful,
                # so we don't need to do extra check, and can safely clear the cache here.
                logger.info("cleanup ota-cache on successful ota-update...")
                self._ota_proxy.stop(cleanup_cache=True)

            logger.info("finish cleanup, signal ota_client to reboot...")
            assert _next == fsm._END

    async def _get_subecu_status(
        self,
        response: v2.StatusResponse,
        *,
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

                    logger.debug(f"{ecu_id} currently is UNAVAILABLE")
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
                        # NOTE: response.available_ecu_ids in sub ecu status is ignored.
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
                self._cached_status = response
                self._cached_status_cv.notify()
            self._status_pulling_lock.release()

            return ret

        else:
            # there is an on-going pulling, use the cache or wait for the result from it
            with self._cached_status_cv:
                self._cached_status_cv.wait_for(lambda: self._cached_status)
                response.CopyFrom(self._cached_status)
            return self._cached_if_subecu_ready

    async def _pulling_subecu_status(self, failed_ecu_list: list) -> Tuple[bool, bool]:
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

        all_subecus_reachable = await self._get_subecu_status(
            response, failed_ecu=failed_ecu_list
        )

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

    async def _ensure_subecu_status(self):
        """
        loop pulling subECUs' status recursively, until all the subECUs are in certain status
        """

        if not self._ecu_info.get_secondary_ecus():
            return  # return when no subecu is attached

        while True:
            # pulling interval
            await asyncio.sleep(server_cfg.LOOP_QUERYING_SUBECU_STATUS_INTERVAL)

            failed_ecu_list = []
            all_subecu_reachable, keep_pulling = await self._pulling_subecu_status(
                failed_ecu_list
            )

            if all_subecu_reachable:
                if not keep_pulling:
                    return
            else:
                # unreachable directly connected subECUs presented
                logger.info(
                    f"retry pulling subECUs status due to unreachable subECUs {failed_ecu_list}"
                )

                """
                if retry_count > retry:
                    # there is at least one subecu is unreachable, even with retrying n times
                    logger.debug(f"unreachable subECU list: {failed_ecu_list}")
                    raise OtaErrorUnrecoverable(
                        f"failed to contact subECUs: {failed_ecu_list}"
                    )
                """
