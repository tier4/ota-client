import asyncio
import threading
import time
import grpc
import grpc.aio
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from multiprocessing import Process
from typing import Any, Dict, List, Optional, Set, Tuple

from app.boot_control import get_boot_controller
from app.create_standby import get_standby_slot_creator
from app.errors import OTAUpdateError

from app.proto import v2
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
        self._lock = threading.Lock()
        self._closed = True
        self._server_p: Optional[Process] = None
        # an event for ota_cache to signal ota_service that
        # cache scrub finished.
        self._scrub_cache_event = multiprocessing.Event()

    @staticmethod
    def launch_entry(init_cache, *, scrub_cache_event):
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

        asyncio.run(_start_uvicorn(init_cache, scrub_cache_event=scrub_cache_event))

    def is_running(self) -> bool:
        return not self._closed

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
                    target=self.launch_entry,
                    args=[init_cache],
                    kwargs={"scrub_cache_event": self._scrub_cache_event},
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


class _UpdateSession:
    def __init__(
        self,
        *,
        executor: ThreadPoolExecutor,
        ota_proxy: Optional[OtaProxyWrapper] = None,
    ) -> None:
        self.update_request = None
        self._lock = asyncio.Lock()
        self._loop = asyncio.get_running_loop()
        self._executor = executor

        # session aware attributes
        self._started = False
        self.fsm = OTAUpdateFSM()

        # local ota_proxy server
        self._ota_proxy: Optional[OtaProxyWrapper] = ota_proxy

    def is_started(self) -> bool:
        return self._started

    async def start(self, request, *, init_cache: bool = False):
        async with self._lock:
            if not self._started:
                self._started = True
                self.fsm = OTAUpdateFSM()
                self.update_request = request

                if self._ota_proxy:
                    await self._loop.run_in_executor(
                        self._executor,
                        partial(
                            self._ota_proxy.start,
                            init_cache=init_cache,
                            wait_on_scrub=True,
                        ),
                    )
                self.fsm.stub_pre_update_ready()
            else:
                logger.warning("ignore overlapping update request")

    async def stop(self, *, update_succeed: bool):
        async with self._lock:
            if self._started:
                logger.info(f"stopping ota update session for {self.update_request=}")
                if self._ota_proxy:
                    logger.info(
                        f"stopping ota_proxy(cleanup_cache={update_succeed})..."
                    )
                    await self._loop.run_in_executor(
                        self._executor,
                        partial(
                            self._ota_proxy.stop,
                            cleanup_cache=update_succeed,
                        ),
                    )

                self._started = False

    async def update_tracker(
        self,
        *,
        my_ecu_update_fut: Optional[asyncio.Future] = None,
        subecu_tracking_task: Optional[asyncio.Task] = None,
    ):
        myecu_succeed, subecus_succeed = False, False
        update_succeed = False
        try:
            if subecu_tracking_task:
                subecus_succeed, failed_list = await subecu_tracking_task
                logger.info(
                    f"all subecus update result: {subecus_succeed}, "
                    f"failed_ecus={failed_list}"
                )
            else:
                subecus_succeed = True

            if my_ecu_update_fut:
                try:
                    await my_ecu_update_fut
                except OTAUpdateError:
                    logger.exception("local update failed")
                    myecu_succeed = False
            else:
                myecu_succeed = True

            update_succeed = myecu_succeed and subecus_succeed
            await self.stop(update_succeed=update_succeed)
            logger.info(
                f"update result for {self.update_request}: "
                f"{update_succeed}({myecu_succeed=}, {subecus_succeed=})"
            )
            self.fsm.stub_cleanup_finished()
        except Exception:
            logger.exception("update tracker failed")
        finally:
            await self.stop(update_succeed=False)


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
        self._status_pulling_lock = asyncio.Lock()
        self._last_status_query = 0
        self._cached_status: v2.StatusResponse = v2.StatusResponse()

        # ota local proxy
        _ota_proxy = None
        if proxy_cfg.enable_local_ota_proxy:
            _ota_proxy = OtaProxyWrapper()
        # update session tracker
        self._update_session = _UpdateSession(
            ota_proxy=_ota_proxy,
            executor=self._executor,
        )

    @staticmethod
    def _find_request(update_request, ecu_id):
        for request in update_request:
            if request.ecu_id == ecu_id:
                return request
        return None

    async def _query_subecu_status(self, tracked_ecu_id: Set[str]) -> v2.StatusResponse:
        response = v2.StatusResponse()

        secondary_ecu = []
        for ecu in self._ecu_info.get_secondary_ecus():
            if ecu["ecu_id"] in tracked_ecu_id:
                secondary_ecu.append(ecu)

        if not secondary_ecu:
            return response

        tasks: List[asyncio.Task] = []
        for secondary_ecu in secondary_ecu:
            tasks.append(
                asyncio.create_task(
                    self._ota_client_call.status(
                        v2.StatusRequest(), secondary_ecu["ip_addr"]
                    ),
                    name=secondary_ecu["ecu_id"],
                )
            )
        if tasks:
            done, pending = await asyncio.wait(
                tasks, timeout=server_cfg.QUERYING_SUBECU_STATUS_TIMEOUT
            )
            for t in done:
                ecu_id = t.get_name()
                if exp := t.exception():
                    logger.debug(f"query subecu: {ecu_id} currently is UNAVAILABLE")
                    if isinstance(exp, grpc.aio.AioRpcError):
                        if exp.code() == grpc.StatusCode.UNAVAILABLE:
                            # request was not received.
                            logger.debug(f"{ecu_id} did not receive the request")
                        else:
                            # other grpc error
                            logger.debug(f"contacting {ecu_id} failed with grpc error")
                    # prepare response
                    ecu = response.ecu.add()
                    ecu.ecu_id = ecu_id
                    ecu.result = v2.UNRECOVERABLE  # TODO: unrecoverable?
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
                logger.warning(f"{ecu_id=} maybe UNAVAILABLE due to connection timeout")
                # TODO: should we record these ECUs as FAILURE in the response?
                ecu = response.ecu.add()
                ecu.ecu_id = ecu_id
                ecu.result = v2.UNRECOVERABLE  # TODO: unrecoverable?

        return response

    async def _ensure_tracked_ecu_ready(
        self, tracked_ecu_id: Set[str]
    ) -> Tuple[bool, List[str]]:
        """Return untill all subecus are not in UPDATE status.

        Return:
            A bool indicates whether all the subecus are in SUCCESS staus,
            and a list of failed ecus if any.
        """
        failed_ecu: Set[str] = set()
        while True:
            _pending_ecu = tracked_ecu_id.copy()

            resp = await self._query_subecu_status(tracked_ecu_id)
            for ecu in resp.ecu:
                ecu_id, ecu_status = ecu.ecu_id, ecu.status.status
                if ecu_status == v2.SUCCESS:
                    _pending_ecu.discard(ecu_id)
                    failed_ecu.discard(ecu_id)
                elif ecu_status == v2.FAILURE:
                    _pending_ecu.discard(ecu_id)
                    failed_ecu.add(ecu_id)

            if not _pending_ecu:
                logger.info(
                    "all subecus are in SUCCESS or Failed status, "
                    f"failed ecu: {failed_ecu}"
                )
                return len(failed_ecu) > 0, list(failed_ecu)

            await asyncio.sleep(server_cfg.LOOP_QUERYING_SUBECU_STATUS_INTERVAL)

    ###### API stub method #######
    def host_addr(self):
        return self._ecu_info.get_ecu_ip_addr()

    async def update(self, request: v2.UpdateRequest) -> v2.UpdateResponse:
        logger.info(f"receive update request: {request}")
        response = v2.UpdateResponse()
        my_ecu_id = self._ecu_info.get_ecu_id()

        if self._update_session.is_started():
            ecu = response.ecu.add()
            ecu.ecu_id = my_ecu_id
            ecu.result = v2.RECOVERABLE

            return response
        else:
            # start this update session
            _init_cache = (
                self._ota_client.live_ota_status.get_ota_status()
                == OTAStatusEnum.SUCCESS
            )
            await self._update_session.start(request, init_cache=_init_cache)

        # a list of directly connected ecus in the update request
        update_tracked_ecus_set: Set[str] = set()

        # secondary ecus
        tasks: List[asyncio.Task] = []
        secondary_ecus = self._ecu_info.get_secondary_ecus()
        logger.info(f"directly connected subecu: {secondary_ecus=}")
        # simultaneously dispatching update requests to all subecus without blocking
        for secondary in secondary_ecus:
            _ecu_id, _ecu_ip = secondary["ecu_id"], secondary["ip_addr"]
            if OtaClientStub._find_request(request.ecu, _ecu_id):
                update_tracked_ecus_set.add(_ecu_id)  # add to tracked ecu list
                tasks.append(
                    asyncio.create_task(
                        self._ota_client_call.update(request, _ecu_ip),
                        # register the task name with sub_ecu id
                        name=_ecu_id,
                    )
                )

        # wait for all sub ecu acknowledge ota update requests
        subecu_tracking_task: Optional[asyncio.Task] = None
        if tasks:
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
                ecu_id = t.get_name()
                if exp := t.exception():
                    logger.error(f"connect sub ecu {ecu_id} failed: {exp!r}")
                    sub_ecu = response.ecu.add()
                    sub_ecu.ecu_id = ecu_id
                    sub_ecu.result = v2.UNRECOVERABLE  # TODO: unrecoverable?
                else:
                    # append subecu's and its subecus' status
                    for e in t.result().ecu:
                        ecu = response.ecu.add()
                        ecu.CopyFrom(e)

            # create a subecu tracking task
            subecu_tracking_task = asyncio.create_task(
                self._ensure_tracked_ecu_ready(update_tracked_ecus_set)
            )

        # after all subecus ack the update request, update my ecu
        my_ecu_update_fut: Optional[asyncio.Future] = None
        if entry := OtaClientStub._find_request(request.ecu, my_ecu_id):
            if not self._ota_client.live_ota_status.request_update():
                logger.warning(
                    f"ota_client should not take ota update under ota_status={self._ota_client.live_ota_status.get_ota_status()}"
                )
                ecu = response.ecu.add()
                ecu.ecu_id = my_ecu_id
                ecu.result = v2.RECOVERABLE
            else:
                logger.info(f"{my_ecu_id=}, {entry=}")
                # dispatch update to local otaclient
                _loop = asyncio.get_running_loop()
                my_ecu_update_fut = _loop.run_in_executor(
                    self._executor,
                    partial(
                        self._ota_client.update,
                        entry.version,
                        entry.url,
                        entry.cookies,
                        fsm=self._update_session.fsm,
                    ),
                )

                ecu = response.ecu.add()
                ecu.ecu_id = my_ecu_id
                ecu.result = v2.NO_FAILURE

        # NOTE(20220513): is it illegal that ecu itself is not requested to update
        # but its subecus do, but currently we just log errors for it
        else:
            logger.warning("entries in update request doesn't include this ecu")

        # start the update tracking in the background
        # NOTE: cleanup is done in the update tracker
        asyncio.create_task(
            self._update_session.update_tracker(
                my_ecu_update_fut=my_ecu_update_fut,
                subecu_tracking_task=subecu_tracking_task,
            )
        )

        logger.debug(f"update requests response: {response}")
        return response

    async def rollback(self, request) -> v2.RollbackResponse:
        logger.info(f"{request=}")
        my_ecu_id = self._ecu_info.get_ecu_id()
        response = v2.RollbackResponse()

        # dispatch rollback requests to all secondary ecus
        secondary_ecus = self._ecu_info.get_secondary_ecus()
        logger.info(f"rollback: {secondary_ecus=}")
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

        # after all subecus response the request, rollback my ecu
        if entry := OtaClientStub._find_request(request.ecu, my_ecu_id):
            if self._ota_client.live_ota_status.request_rollback():
                logger.info(f"{my_ecu_id=}, {entry=}")
                # dispatch the rollback request to threadpool
                self._executor.submit(self._ota_client.rollback)

                # rollback request is permitted, prepare the response
                ecu = response.ecu.add()
                ecu.ecu_id = my_ecu_id
                ecu.result = v2.NO_FAILURE
            else:
                # current ota_client's status indicates that
                # the ota_client should not start an ota rollback
                logger.warning(
                    "ota_client should not take ota rollback under "
                    f"ota_status={self._ota_client.live_ota_status.get_ota_status()}"
                )
                ecu = response.ecu.add()
                ecu.ecu_id = my_ecu_id
                ecu.result = v2.RECOVERABLE
        else:
            logger.warning("entries in rollback request doesn't include this ecu")

        return response

    async def status(self, _: Optional[v2.StatusRequest] = None) -> v2.StatusResponse:
        """
        NOTE: if the cached status is older than <server_cfg.QUERY_SUBECU_STATUS_INTERVAL>,
        the caller should take the responsibility to update the cached status.
        """
        cur_time = time.time()
        if (
            cur_time - self._last_status_query
            <= server_cfg.QUERY_SUBECU_STATUS_INTERVAL
        ):
            # within query interval, use cached status
            response = v2.StatusResponse()
            response.CopyFrom(self._cached_status)
            return response

        # the caller should take the responsibility to update the status
        # for status API, query all directly connected subecus
        tracked_ecu = set([ecu.id for ecu in self._ecu_info.get_secondary_ecus()])
        async with self._status_pulling_lock:
            self._last_status_query = cur_time
            # prepare subecu status
            response = await asyncio.wait_for(
                self._query_subecu_status(tracked_ecu),
                timeout=server_cfg.WAITING_GET_SUBECU_STATUS,
            )

            # prepare my ecu status
            ecu_id = self._ecu_info.get_ecu_id()  # my ecu id
            ecu = response.ecu.add()
            ecu.ecu_id = ecu_id
            if status := self._ota_client.status():
                # construct status response
                ecu.result = v2.NO_FAILURE
                ecu.status.status = getattr(v2.StatusOta, status.status)
                ecu.status.failure = getattr(v2.FailureType, status.failure_type)
                ecu.status.failure_reason = status.failure_reason
                ecu.status.version = status.version

                prg = ecu.status.progress
                prg.CopyFrom(_statusprogress_msg_from_dict(status.update_progress))
                if phase := status.get_update_phase():
                    prg.phase = getattr(v2.StatusProgressPhase, phase)
            else:
                # otaclient status method doesn't return valid result
                ecu.result = v2.RECOVERABLE

            # available ecu ids
            available_ecu_ids = self._ecu_info.get_available_ecu_ids()
            response.available_ecu_ids.extend(available_ecu_ids)

            # register the status to cache
            cached_response = v2.StatusResponse()
            cached_response.CopyFrom(response)
            self._cached_status = cached_response
            return response
