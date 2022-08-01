import asyncio
import grpc
import grpc.aio
import multiprocessing
import threading
import time

from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from functools import partial
from multiprocessing import Process
from typing import Callable, Coroutine, Dict, List, Optional

from app.boot_control import get_boot_controller
from app.create_standby import get_standby_slot_creator
from app.ecu_info import EcuInfo
from app.ota_status import OTAStatusEnum
from app.ota_client import OTAClient, OTAUpdateFSM
from app.ota_client_call import OtaClientCall
from app.proto import v2
from app.proxy_info import proxy_cfg

from app.configs import BOOT_LOADER, server_cfg, config as cfg
from app import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


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
        import shutil

        with self._lock:
            if not self._closed and self._server_p:
                # send SIGTERM to the server process
                self._server_p.terminate()
                self._server_p.join(timeout=16)  # wait for ota_proxy cleanup
                self._closed = True
                logger.info("ota proxy server closed")

                if cleanup_cache:
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
                    logger.info("ota_proxy server launched")
                self.fsm.stub_pre_update_ready()
            else:
                logger.warning("ignore overlapping update request")

    async def stop(self, *, update_succeed: bool):
        async with self._lock:
            if self._started:
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

    @classmethod
    async def my_ecu_update_tracker(
        cls,
        *,
        ota_client_get_exp: Callable,
        fsm: OTAUpdateFSM,
        executor,
    ) -> bool:
        """
        Params:
            otaclient_get_exp: a callable that can retrieve otaclient last exp
        """
        _loop = asyncio.get_running_loop()
        await _loop.run_in_executor(executor, fsm.stub_wait_for_local_update)

        if ota_client_get_exp:
            if await _loop.run_in_executor(executor, ota_client_get_exp):
                return False
        return True

    async def update_tracker(
        self,
        *,
        my_ecu_tracking_task: Optional[Coroutine] = None,
        subecu_tracking_task: Optional[Coroutine] = None,
    ):
        """
        NOTE: expect input tracking task to return a bool to indicate
              whether the tracked task successfully finished or not.
        """

        subecus_succeed = True
        if subecu_tracking_task:
            subecus_succeed = await subecu_tracking_task
            logger.info(f"subecus update result: {subecus_succeed}")

        my_ecu_succeed = True
        if my_ecu_tracking_task:
            my_ecu_succeed = await my_ecu_tracking_task
            logger.info(f"my ecu update result: {my_ecu_succeed}")

        update_succeed = my_ecu_succeed and subecus_succeed
        await self.stop(update_succeed=update_succeed)
        self.fsm.stub_cleanup_finished()


class _SubECUTracker:
    """Loop pulling subecus until all the tracked subecus become
        SUCCESS, FAILURE.

    NOTE: if subecu is unreachable, this tracker will still keep pulling,
        the end user should interrupt the update/rollback session by their own.
    """

    class ECUStatus(Enum):
        SUCCESS = "success"
        FAILURE = "failure"
        NOT_READY = "not_ready"
        API_FAILED = "api_failed"
        UNREACHABLE = "unreachable"

    def __init__(self, tracked_ecus_dict: Dict[str, str]) -> None:
        self.subecu_query_timeout = server_cfg.QUERYING_SUBECU_STATUS_TIMEOUT
        self.interval = server_cfg.LOOP_QUERYING_SUBECU_STATUS_INTERVAL
        self.tracked_ecus_dict: Dict[str, str] = tracked_ecus_dict
        self._ota_client_call = OtaClientCall(server_cfg.SERVER_PORT)

    async def _query_ecu(self, ecu_addr: str) -> ECUStatus:
        """Query ecu status API once."""
        try:
            resp: v2.StatusResponse = await asyncio.wait_for(
                self._ota_client_call.status(v2.StatusRequest(), ecu_addr),  # type: ignore
                timeout=self.subecu_query_timeout,
            )

            # if ANY OF the subecu and its child ecu are
            # not in SUCCESS/FAILURE otastatus, or unreacable, return False
            all_ready, all_success = True, True
            for ecu in resp.ecu:
                if ecu.result != v2.NO_FAILURE:
                    all_ready = False
                    break

                _status = ecu.status.status
                if _status in (v2.SUCCESS, v2.FAILURE):
                    all_success &= _status == v2.SUCCESS
                else:
                    all_ready = False
                    break

            if all_ready:
                if all_success:
                    # this ecu and its subecus are all in SUCCESS
                    return self.ECUStatus.SUCCESS
                # one of this ecu and its subecu in FAILURE
                return self.ECUStatus.FAILURE
            # one of this ecu and its subecu are still in UPDATING/ROLLBACKING
            return self.ECUStatus.NOT_READY
        except (grpc.aio.AioRpcError, asyncio.TimeoutError):
            # this ecu is unreachable
            return self.ECUStatus.UNREACHABLE

    async def ensure_tracked_ecu_ready(self) -> bool:
        """Loop pulling tracked ecus' status until are ready.

        Return:
            A bool to indicate if all subecus and its child are in SUCCESS status.

        NOTE: 1. ready means otastatus in SUCCESS or FAILURE,
              2. this method will block until all tracked ecus are ready,
              2. if subecu is unreachable, this tracker will still keep pulling.
        """
        while True:
            coros: List[Coroutine] = []
            for _, subecu_addr in self.tracked_ecus_dict.items():
                coros.append(self._query_ecu(subecu_addr))

            all_ecus_success, all_ecus_ready = True, True
            for _ecustatus in await asyncio.gather(*coros):
                this_subecu_ready = True
                if _ecustatus in (
                    self.ECUStatus.SUCCESS,
                    self.ECUStatus.FAILURE,
                ):
                    all_ecus_success &= _ecustatus == self.ECUStatus.SUCCESS
                else:
                    # if any of this ecu's subecu(or itself) is not ready,
                    # then this subecu is not ready
                    this_subecu_ready = False

                all_ecus_ready &= this_subecu_ready

            if all_ecus_ready:
                return all_ecus_success
            await asyncio.sleep(self.interval)


class OtaClientStub:
    def __init__(self):
        # dispatch the requested operations to threadpool
        self._executor = ThreadPoolExecutor(thread_name_prefix="ota_client_stub")

        self._ecu_info = EcuInfo()
        self.my_ecu_id = self._ecu_info.get_ecu_id()
        self.subecus_dict: Dict[str, str] = {
            e["ecu_id"]: e["ip_addr"] for e in self._ecu_info.get_secondary_ecus()
        }

        # NOTE: explicitly specific which mechanism to use
        # for boot control and create standby slot
        self._ota_client = OTAClient(
            boot_control_cls=get_boot_controller(BOOT_LOADER),
            create_standby_cls=get_standby_slot_creator(cfg.STANDBY_CREATION_MODE),
            my_ecu_id=self.my_ecu_id,
        )
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

    ###### API stub method #######
    def host_addr(self):
        return self._ecu_info.get_ecu_ip_addr()

    async def update(self, request: v2.UpdateRequest) -> v2.UpdateResponse:
        logger.debug(f"receive update request: {request}")
        response = v2.UpdateResponse()

        if self._update_session.is_started():
            ecu = response.ecu.add()
            ecu.ecu_id = self.my_ecu_id
            ecu.result = v2.RECOVERABLE

            logger.debug("ignore duplicated update request")
            return response
        else:
            # start this update session
            _init_cache = (
                self._ota_client.live_ota_status.get_ota_status()
                == OTAStatusEnum.SUCCESS
            )
            await self._update_session.start(request, init_cache=_init_cache)

        # a list of directly connected ecus in the update request
        tracked_ecus_dict: Dict[str, str] = {}

        # simultaneously dispatching update requests to all subecus without blocking
        coros: List[Coroutine] = []
        for _ecu_id, _ecu_ip in self.subecus_dict.items():
            if OtaClientStub._find_request(request.ecu, _ecu_id):
                # add to tracked ecu list
                tracked_ecus_dict[_ecu_id] = _ecu_ip
                coros.append(
                    self._ota_client_call.update_call(
                        _ecu_id,
                        _ecu_ip,
                        request=request,
                        timeout=server_cfg.WAITING_SUBECU_ACK_UPDATE_REQ_TIMEOUT,
                    )
                )
        logger.info(f"update: {tracked_ecus_dict=}")

        # wait for all sub ecu acknowledge ota update requests
        update_resp: v2.UpdateResponse
        for update_resp in await asyncio.gather(*coros):
            for _ecu_resp in update_resp.ecu:  # type: ignore
                _ecu = response.ecu.add()
                _ecu.CopyFrom(_ecu_resp)

        # after all subecus ack the update request, update my ecu
        my_ecu_tracking_task: Optional[Coroutine] = None
        if entry := OtaClientStub._find_request(request.ecu, self.my_ecu_id):
            if not self._ota_client.live_ota_status.request_update():
                logger.warning(
                    f"ota_client should not take ota update under ota_status={self._ota_client.live_ota_status.get_ota_status()}"
                )
                ecu = response.ecu.add()
                ecu.ecu_id = self.my_ecu_id
                ecu.result = v2.RECOVERABLE
            else:
                logger.info(f"update my_ecu: {self.my_ecu_id=}, {entry=}")
                # dispatch update to local otaclient
                _loop = asyncio.get_running_loop()
                _loop.run_in_executor(
                    self._executor,
                    partial(
                        self._ota_client.update,
                        entry.version,
                        entry.url,
                        entry.cookies,
                        fsm=self._update_session.fsm,
                    ),
                )

                # launch the local update tracker
                # NOTE: pass otaclient's get_last_failure method into the tracker
                my_ecu_tracking_task = _UpdateSession.my_ecu_update_tracker(
                    executor=self._executor,
                    ota_client_get_exp=self._ota_client.get_last_failure,
                    fsm=self._update_session.fsm,
                )

                ecu = response.ecu.add()
                ecu.ecu_id = self.my_ecu_id
                ecu.result = v2.NO_FAILURE

        # NOTE(20220513): is it illegal that ecu itself is not requested to update
        # but its subecus do, but currently we just log errors for it
        else:
            logger.warning("entries in update request doesn't include this ecu")

        # start the update tracking in the background
        # NOTE: cleanup is done in the update tracker
        # create a subecu tracking task
        subecu_tracking_task = _SubECUTracker(
            tracked_ecus_dict
        ).ensure_tracked_ecu_ready()

        asyncio.create_task(
            self._update_session.update_tracker(
                my_ecu_tracking_task=my_ecu_tracking_task,
                subecu_tracking_task=subecu_tracking_task,
            )
        )

        logger.debug(f"update requests response: {response}")
        return response

    async def rollback(self, request: v2.RollbackRequest) -> v2.RollbackResponse:
        logger.info(f"{request=}")
        response = v2.RollbackResponse()

        # wait for all subecus to ack the requests
        tracked_ecus_dict: Dict[str, str] = {}
        coros: List[Coroutine] = []
        for _ecu_id, _ecu_ip in self.subecus_dict.items():
            if OtaClientStub._find_request(request.ecu, _ecu_id):
                # add to tracked ecu list
                tracked_ecus_dict[_ecu_id] = _ecu_ip
                coros.append(
                    self._ota_client_call.rollback_call(
                        _ecu_id,
                        _ecu_ip,
                        request=request,
                        # TODO: should rollback timeout has its own value?
                        timeout=server_cfg.WAITING_SUBECU_ACK_UPDATE_REQ_TIMEOUT,
                    )
                )
        logger.info(f"rollback: {tracked_ecus_dict=}")

        # wait for all sub ecu acknowledge ota rollback requests
        rollback_resp: v2.UpdateResponse
        for rollback_resp in await asyncio.gather(*coros):
            for _ecu_resp in rollback_resp.ecu:  # type: ignore
                _ecu = response.ecu.add()
                _ecu.CopyFrom(_ecu_resp)

        # after all subecus response the request, rollback my ecu
        if entry := OtaClientStub._find_request(request.ecu, self.my_ecu_id):
            if self._ota_client.live_ota_status.request_rollback():
                logger.info(f"rollback my_ecu: {self.my_ecu_id=}, {entry=}")
                # dispatch the rollback request to threadpool
                self._executor.submit(self._ota_client.rollback)

                # rollback request is permitted, prepare the response
                ecu = response.ecu.add()
                ecu.ecu_id = self.my_ecu_id
                ecu.result = v2.NO_FAILURE
            else:
                # current ota_client's status indicates that
                # the ota_client should not start an ota rollback
                logger.warning(
                    "ota_client should not take ota rollback under "
                    f"ota_status={self._ota_client.live_ota_status.get_ota_status()}"
                )
                ecu = response.ecu.add()
                ecu.ecu_id = self.my_ecu_id
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
        if cur_time - self._last_status_query > server_cfg.STATUS_UPDATE_INTERVAL:
            async with self._status_pulling_lock:
                self._last_status_query = cur_time

                # get subecus' status
                subecus_resp = v2.StatusResponse()
                coros: List[Coroutine] = []
                for subecu_id, subecu_addr in self.subecus_dict.items():
                    coros.append(
                        self._ota_client_call.status_call(
                            subecu_id,
                            subecu_addr,
                            timeout=server_cfg.QUERYING_SUBECU_STATUS_TIMEOUT,
                        )
                    )
                subecu_resp: v2.StatusResponse
                for subecu_resp in await asyncio.gather(*coros):
                    if subecu_resp is None:
                        continue

                    # gather the subecu and its child ecus status
                    for _ecu_resp in subecu_resp.ecu:  # type: ignore
                        _ecu = subecus_resp.ecu.add()
                        _ecu.CopyFrom(_ecu_resp)

                # prepare final response
                resp = v2.StatusResponse()
                resp.available_ecu_ids.extend(self._ecu_info.get_available_ecu_ids())
                ## append from subecus_resp
                for ecu_status in subecus_resp.ecu:
                    ecu = resp.ecu.add()
                    ecu.CopyFrom(ecu_status)
                ## append my_ecu status
                my_ecu = resp.ecu.add()
                if my_ecu_status := self._ota_client.status():
                    my_ecu.CopyFrom(my_ecu_status)
                else:
                    # otaclient status method doesn't return valid result
                    my_ecu.ecu_id = self.my_ecu_id
                    my_ecu.result = v2.RECOVERABLE

                # register the status to cache
                self._cached_status = resp

        # respond with the cached status
        res = v2.StatusResponse()
        res.CopyFrom(self._cached_status)
        return res
