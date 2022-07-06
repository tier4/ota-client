import asyncio
from functools import partial
import grpc
import grpc.aio
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import Process
from threading import Lock, Condition
from typing import Any, Dict, List, Optional, Tuple
from app.boot_control import get_boot_controller
from app.create_standby import get_standby_slot_creator
from app.errors import OTAFailureType

import app.otaclient_v2_pb2 as v2
from app.ota_status import OTAStatusEnum
from app.ota_client import OTAClient, OTAUpdateFSM
from app.ota_client_call import OtaClientCall
from app.proxy_info import proxy_cfg
from app.ecu_info import EcuInfo

from app.configs import BOOT_LOADER, server_cfg, config as cfg
from app import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


def _path_load():
    import sys
    from pathlib import Path

    project_base = Path(__file__).absolute().parent.parent
    sys.path.append(str(project_base))


_path_load()


def _statusprogress_msg_from_dict(input: Dict[str, Any]) -> v2.StatusProgress:
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
        self._server_p: Optional[Process] = None
        # an event for ota_cache to signal ota_service that
        # cache scrub finished.
        self._scrub_cache_event = multiprocessing.Event()

    @staticmethod
    async def _start_uvicorn(init_cache: bool, *, scrub_cache_event):
        import uvicorn
        from ota_proxy import App, OTACache

        _ota_cache = OTACache(
            cache_enabled=proxy_cfg.enable_local_ota_proxy_cache,
            upper_proxy=proxy_cfg.upper_ota_proxy,
            enable_https=proxy_cfg.gateway,
            init_cache=init_cache,
            scrub_cache_event=scrub_cache_event,
        )

        # NOTE: explicitly set loop and http options
        # to prevent using wrong libs
        # NOTE 2: http=="httptools" will break ota_proxy
        config = uvicorn.Config(
            App(_ota_cache),
            host=proxy_cfg.local_ota_proxy_listen_addr,
            port=proxy_cfg.local_ota_proxy_listen_port,
            log_level="error",
            lifespan="on",
            loop="asyncio",
            http="h11",
        )
        server = uvicorn.Server(config)
        await server.serve()

    def start(
        self,
        init_cache: bool,
        *,
        wait_on_scrub=True,
        wait_on_timeout: Optional[float] = None,
    ) -> Optional[int]:
        """Launch ota_proxy as a separate process."""
        with self._lock:
            if self._closed:
                self._server_p = Process(
                    target=asyncio.run,
                    args=[
                        self._start_uvicorn(
                            init_cache, scrub_cache_event=self._scrub_cache_event
                        ),
                    ],
                )
                self._server_p.start()

                self._closed = False
                logger.info(
                    f"ota proxy server started(pid={self._server_p.pid})"
                    f"{proxy_cfg=}"
                )

                if wait_on_scrub:
                    self._scrub_cache_event.wait(timeout=wait_on_timeout)

                return self._server_p.pid
            else:
                logger.warning("try to launch ota-proxy again, ignored")

    def wait_on_ota_cache(self, timeout: Optional[float] = None):
        """Wait on ota_cache to finish initializing."""
        self._scrub_cache_event.wait(timeout=timeout)

    def stop(self, cleanup_cache=False):
        """Shutdown ota_proxy.

        NOTE: cache folder cleanup is done here.
        """
        from ota_proxy.config import config as proxy_srv_cfg

        with self._lock:
            if not self._closed and self._server_p:
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
        self._executor = ThreadPoolExecutor(thread_name_prefix="ota_client_stub")

        # NOTE: explicitly specific which mechanism to use
        # for boot control and create standby slot
        self._ota_client = OTAClient(
            boot_control_cls=get_boot_controller(BOOT_LOADER),
            create_standby_cls=get_standby_slot_creator(cfg.STANDBY_CREATION_MODE),
        )
        self._ecu_info = EcuInfo()
        self._ota_client_call = OtaClientCall(server_cfg.SERVER_PORT)

        # for _get_subecu_status
        self._status_pulling_lock = Lock()
        self._cached_status_cv: Condition = Condition()
        self._cached_status: Optional[v2.StatusResponse] = None
        self._cached_if_subecu_ready: Optional[bool] = None

        # ota proxy server
        if proxy_cfg.enable_local_ota_proxy:
            self._ota_proxy = OtaProxyWrapper()

    def host_addr(self):
        return self._ecu_info.get_ecu_ip_addr()

    async def update(self, request: v2.UpdateRequest) -> v2.UpdateResponse:
        logger.info(f"{request=}")
        my_ecu_id = self._ecu_info.get_ecu_id()
        _loop = asyncio.get_running_loop()

        response = v2.UpdateResponse()
        # TODO: add a method for ota_client to proxy request_update
        if not self._ota_client.live_ota_status.request_update():
            # current ota_client's status indicates that
            # the ota_client should not start an ota update
            logger.warning(
                f"ota_client should not take ota update under ota_status={self._ota_client.live_ota_status.get_ota_status()}"
            )
            ecu = response.ecu.add()
            ecu.ecu_id = my_ecu_id
            ecu.result = v2.RECOVERABLE

            return response
        else:
            # update request permitted, prepare response
            ecu = response.ecu.add()
            ecu.ecu_id = my_ecu_id
            ecu.result = v2.NO_FAILURE

        ### start ota_proxy server if needed
        # init state machine
        ota_sfsm = OTAUpdateFSM()

        # start ota proxy server
        # check current ota_status, if the status is not SUCCESS,
        # always assume there is an interrupted ota update, thus reuse the cache if possible
        # NOTE: now the cache scrubing will block the ota_proxy start,
        # to ensure the proxy is fully ready before sending update requests to the subecus
        # NOTE 2: signal ota_client after ota_proxy launched
        if proxy_cfg.enable_local_ota_proxy:
            _init_cache = (
                self._ota_client.live_ota_status.get_ota_status()
                == OTAStatusEnum.SUCCESS
            )
            # wait for ota_cache to finish initializing
            await _loop.run_in_executor(
                self._executor,
                partial(
                    self._ota_proxy.start,
                    init_cache=_init_cache,
                    wait_on_scrub=True,
                ),
            )
            ota_sfsm.stub_ota_proxy_launched()

        ### dispatch update requests to all the subecus
        # secondary ecus
        tasks: List[asyncio.Task] = []
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

        # wait for all sub ecu acknowledge ota update requests
        if tasks:
            done, pending = await asyncio.wait(
                tasks, timeout=server_cfg.WAITING_SUBECU_ACK_UPDATE_REQ_TIMEOUT
            )
            # indicates whether any of the subecu cannot ack the update request
            _subecu_failed = len(pending) != 0

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
                if exp := t.exception():
                    _subecu_failed = True

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

            if _subecu_failed:
                logger.error(
                    "failed to ensure all directly connected subecus acked the update requests, "
                    "abort update"
                )
                ecu = response.ecu.add()
                ecu.ecu_id = my_ecu_id
                ecu.result = v2.RECOVERABLE

                return response

        # after all subecus ack the update request, update my ecu
        if entry := OtaClientStub._find_request(request.ecu, my_ecu_id):
            logger.info(f"{my_ecu_id=}, {entry=}")
            # dispatch update to local otaclient
            asyncio.create_task(
                self._update_executor(entry, fsm=ota_sfsm),
                name="local_ota_update",
            )

        # NOTE(20220513): is it illegal that ecu itself is not requested to update
        # but its subecus do, but currently we just log errors for it
        else:
            logger.warning("entries in update request doesn't include this ecu")

        logger.info(f"{response=}")
        return response

    async def rollback(self, request) -> v2.RollbackResponse:
        logger.info(f"{request=}")
        my_ecu_id = self._ecu_info.get_ecu_id()

        response = v2.RollbackResponse()
        # TODO: rollback request proxy?
        if not self._ota_client.live_ota_status.request_rollback():
            # current ota_client's status indicates that
            # the ota_client should not start an ota rollback
            logger.warning(
                f"ota_client should not take ota rollback under ota_status={self._ota_client.live_ota_status.get_ota_status()}"
            )
            ecu = response.ecu.add()
            ecu.ecu_id = my_ecu_id
            ecu.result = v2.RECOVERABLE

            return response
        else:
            # rollback request is permitted, prepare the response
            ecu = response.ecu.add()
            ecu.ecu_id = my_ecu_id
            ecu.result = v2.NO_FAILURE

        # dispatch rollback requests to all secondary ecus
        secondary_ecus = self._ecu_info.get_secondary_ecus()
        logger.info(f"{secondary_ecus=}")
        tasks: List[asyncio.Task] = []
        for secondary in secondary_ecus:
            if entry := OtaClientStub._find_request(request.ecu, secondary["ecu_id"]):
                tasks.append(
                    asyncio.create_task(
                        self._ota_client_call.rollback(request, secondary["ip_addr"]),
                        name=secondary["ecu_id"],
                    )
                )

        # wait for all subecus to ack the requests
        # TODO: should rollback timeout has its own value?
        if tasks:
            done, pending = await asyncio.wait(
                tasks, timeout=server_cfg.WAITING_SUBECU_ACK_UPDATE_REQ_TIMEOUT
            )
            # indicates whether any of the subecu cannot ack the rollback request
            _subecu_failed = len(pending) != 0

            for t in pending:
                ecu_id = t.get_name()

                sub_ecu = response.ecu.add()
                sub_ecu.ecu_id = ecu_id
                sub_ecu.result = v2.RECOVERABLE
                logger.error(
                    f"subecu {ecu_id} doesn't respond to ota rollback request on time"
                )
            for t in done:
                if exp := t.exception():
                    _subecu_failed = True

                    ecu_id = t.get_name()
                    logger.error(f"connect sub ecu {ecu_id} failed: {exp!r}")

                    sub_ecu = response.ecu.add()
                    sub_ecu.ecu_id = ecu_id
                    sub_ecu.result = v2.UNRECOVERABLE  # TODO: unrecoverable?
                else:
                    subsub_ecus: List[v2.RollbackResponseEcu] = t.result().ecu
                    # NOTE: subsub_ecus list also include current ecu
                    for e in subsub_ecus:
                        ecu = response.ecu.add()
                        ecu.CopyFrom(e)
                        logger.info(f"{ecu.ecu_id=}, {ecu.result=}")

            if _subecu_failed:
                logger.error(
                    "failed to ensure all directly connected subecus acked the rollback requests, "
                    "abort rollback"
                )
                ecu = response.ecu.add()
                ecu.ecu_id = my_ecu_id
                ecu.result = v2.RECOVERABLE

                return response

        # after all subecus response the request, rollback my ecu
        if entry := OtaClientStub._find_request(request.ecu, my_ecu_id):
            logger.info(f"{my_ecu_id=}, {entry=}")
            # dispatch the rollback request to threadpool
            self._executor.submit(self._ota_client.rollback)
        else:
            logger.warning("entries in rollback request doesn't include this ecu")

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

        ecu = response.ecu.add()
        ecu.ecu_id = ecu_id
        if status := self._ota_client.status():
            # construct status response
            ecu.result = OTAFailureType.NO_FAILURE.value
            ecu.status.status = v2.StatusOta.Value(status.status)
            ecu.status.failure = v2.FailureType.Value(status.failure_type)
            ecu.status.failure_reason = status.failure_reason
            ecu.status.version = status.version

            prg = ecu.status.progress
            prg.CopyFrom(_statusprogress_msg_from_dict(status.update_progress))
            if phase := status.get_update_phase():
                prg.phase = v2.StatusProgressPhase.Value(phase)
        else:
            # otaclient status method doesn't return valid result
            ecu.result = OTAFailureType.RECOVERABLE.value

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

    async def _update_executor(self, entry, *, fsm: OTAUpdateFSM):
        """
        entry for local ota update

        NOTE: no exceptions will be raised as there is no upper caller after the update is dispatched.
        exceptions will only be recorded by logger. Use status API to query the status of the update.
        """
        _loop = asyncio.get_running_loop()

        # dispatch the local update to threadpool
        _loop.run_in_executor(
            self._executor,
            partial(
                self._ota_client.update,
                entry.version,
                entry.url,
                entry.cookies,
                fsm=fsm,
            ),
        )

        # wait for local ota update and all subecus to finish update, and then do cleanup
        # NOTE: the failure of ota_client side update will not reflect immediately here,
        # however it is not a problem as we will track the update state via status API.

        # ensure all subecus state
        await self._ensure_subecu_status()
        logger.info("all subECUs are updated and become ready")

        # wait for local update to finish
        await _loop.run_in_executor(
            self._executor,
            fsm.stub_wait_for_local_update,
        )

        # check the last failure AFTER local update finished,
        # (after IN_UPDATE stage, before POST_UPDATE stage)
        update_success = self._ota_client.get_last_failure() is None
        if proxy_cfg.enable_local_ota_proxy:
            logger.info(
                "stop ota_proxy server on all subecus and my ecu update finished,"
            )
            # cleanup cache only when the local update is successful
            await _loop.run_in_executor(
                self._executor,
                partial(
                    self._ota_proxy.stop,
                    cleanup_cache=update_success,
                ),
            )

        # signal the otaclient to execute post update
        logger.info(f"local update result: {update_success=}")
        fsm.stub_cleanup_finished()

    async def _get_subecu_status(
        self,
        response: v2.StatusResponse,
        *,
        failed_ecu: Optional[List[str]] = None,  # output
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
            tasks: List[asyncio.Task] = []
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

                if exp := t.exception():
                    # exception raised from the task
                    failed_ecu.append(ecu_id)

                    logger.debug(f"{ecu_id} currently is UNAVAILABLE")
                    if isinstance(exp, grpc.aio.AioRpcError):
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
                ecu_id = t.get_name()

                # task timeout
                failed_ecu.append(ecu_id)
                logger.warning(f"{ecu_id=} maybe UNAVAILABLE due to connection timeout")
                # TODO: should we record these ECUs as FAILURE in the response?
                ecu = response.ecu.add()
                ecu.ecu_id = ecu_id
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
            if e.result != v2.FailureType.NO_FAILURE:
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
                elif st == v2.StatusOta.FAILURE:
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
                raise ValueError(
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
