# Copyright 2022 TIER IV, INC. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import asyncio
import gc
import logging
import multiprocessing
import threading
import time
import shutil
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from functools import partial
from pathlib import Path
from typing import Coroutine, Dict, List, Optional, Generator

from . import log_setting
from .boot_control import BootloaderType, get_boot_controller, detect_bootloader
from .configs import server_cfg, config as cfg
from .create_standby import get_standby_slot_creator
from .ecu_info import ECUInfo
from .ota_client import OTAClient, OTAUpdateFSM
from .ota_client_call import ECUNoResponse, OtaClientCall
from .proto import wrapper
from .proto.wrapper import StatusRequest, StatusResponse
from .proxy_info import proxy_cfg

from otaclient.ota_proxy.config import config as proxy_srv_cfg
from otaclient.ota_proxy import App, OTACache, OTACacheScrubHelper

logger = log_setting.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class OtaProxyWrapper:
    def __init__(self):
        self._lock = threading.Lock()
        self._closed = True
        self._launcher_gen: Generator = None  # type: ignore

    @staticmethod
    def _start_otaproxy(init_cache):
        """Main entry for ota_proxy in separate process.

        NOTE: logging needs to be configured again.
        """
        import uvloop
        import uvicorn

        # configure logging for ota_proxy process
        log_setting.configure_logging(
            loglevel=logging.CRITICAL, http_logging_url=log_setting.get_ecu_id()
        )
        _otaproxy_logger = logging.getLogger("otaclient.ota_proxy")
        _otaproxy_logger.setLevel(cfg.DEFAULT_LOG_LEVEL)

        async def _start_uvicorn(init_cache: bool):
            _ota_cache = OTACache(
                cache_enabled=proxy_cfg.enable_local_ota_proxy_cache,
                upper_proxy=proxy_cfg.upper_ota_proxy,
                enable_https=proxy_cfg.gateway,
                init_cache=init_cache,
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

        uvloop.install()
        asyncio.run(_start_uvicorn(init_cache))

    def _launcher(self, *, init_cache: bool):
        _mp_ctx = multiprocessing.get_context("spawn")
        _server_p = _mp_ctx.Process(
            target=self._start_otaproxy,
            args=[init_cache],
        )
        try:
            _server_p.start()
            logger.info(f"ota proxy server started(pid={_server_p.pid}, {proxy_cfg=})")
            # wait for stop
            _cleanup_cache = yield _server_p.pid
            logger.info(f"closing otaproxy: {_cleanup_cache=}")
            _server_p.terminate()
            _server_p.join(timeout=16)
            _server_p.close()
            if _cleanup_cache:
                shutil.rmtree(proxy_srv_cfg.BASE_DIR, ignore_errors=True)
        finally:
            self._closed, _server_p = True, None
            logger.info("ota proxy server closed")

    def start(self, init_cache: bool) -> Optional[int]:
        """Launch ota_proxy as a separate process."""
        if self._lock.acquire(blocking=False) and self._closed:
            self._closed = False
            try:
                # NOTE, TODO: currently we always wait for otacache scrubing
                #             before launching the otaproxy
                _cache_base_dir = Path(proxy_srv_cfg.BASE_DIR)
                _cache_db_file = Path(proxy_srv_cfg.DB_FILE)

                _should_init_cache = init_cache or not (
                    _cache_base_dir.is_dir() and _cache_db_file.is_file()
                )
                if not _should_init_cache:
                    _scrub_helper = OTACacheScrubHelper(_cache_db_file, _cache_base_dir)
                    try:
                        _scrub_helper.scrub_cache()
                    except Exception as e:
                        logger.error(
                            f"failed to complete cache scrub: {e!r}, force init cache"
                        )
                        _should_init_cache = True
                    finally:
                        del _scrub_helper

                self._launcher_gen = self._launcher(init_cache=_should_init_cache)
                return next(self._launcher_gen)
            except Exception as e:
                logger.error(f"failed to start otaproxy: {e!r}")
                self._launcher_gen = None  # type: ignore
            finally:
                self._lock.release()
        else:
            logger.warning("try to launch otaproxy again, ignored")

    def stop(self, cleanup_cache=False):
        """Shutdown ota_proxy.

        NOTE: cache folder cleanup is done in shutdown process.
        """
        if (
            self._lock.acquire(blocking=False)
            and not self._closed
            and self._launcher_gen
        ):
            logger.info("shutdown otaproxy...")
            try:
                self._launcher_gen.send(cleanup_cache)
            except StopIteration:
                pass  # expected when gen finished
            finally:
                self._closed, self._launcher_gen = True, None  # type: ignore
                self._lock.release()
        else:
            logger.warning("ignored unproper otaproxy shutdown request")


class _RequestHandlingSession:
    def __init__(
        self,
        *,
        executor: ThreadPoolExecutor,
        enable_otaproxy: bool,
    ) -> None:
        self.update_request = None
        self._lock = asyncio.Lock()
        self._executor = executor

        # session aware attributes
        self._started = asyncio.Event()
        self.fsm = OTAUpdateFSM()

        # local ota_proxy server
        self.enable_otaproxy = enable_otaproxy
        self._ota_proxy: Optional[OtaProxyWrapper] = None

    @property
    def is_running(self) -> bool:
        return self._started.is_set()

    async def start(self, request, *, init_cache: bool = False):
        async with self._lock:
            if not self._started.is_set():
                self._started.set()
                self.fsm = OTAUpdateFSM()
                self.update_request = request

                if self.enable_otaproxy:
                    self._ota_proxy = OtaProxyWrapper()
                    try:
                        await asyncio.get_running_loop().run_in_executor(
                            self._executor,
                            self._ota_proxy.start,
                            init_cache,
                        )
                        logger.info("ota_proxy server launched")
                        self.fsm.stub_pre_update_ready()
                    except Exception as e:
                        logger.error(f"failed to start ota_proxy: {e!r}")
                        self.fsm.on_otaservice_failed()

    async def stop(self, *, update_succeed: bool):
        async with self._lock:
            if self._started.is_set() and self._ota_proxy:
                logger.info(f"stopping ota_proxy(cleanup_cache={update_succeed})...")
                try:
                    await asyncio.get_running_loop().run_in_executor(
                        self._executor,
                        self._ota_proxy.stop,
                        update_succeed,
                    )
                except Exception as e:
                    logger.error(f"failed to stop ota_proxy gracefully: {e!r}")
                    self.fsm.on_otaservice_failed()
                finally:
                    self._ota_proxy = None
                    gc.collect()

            self._started.clear()

    @classmethod
    async def my_ecu_update_tracker(
        cls,
        *,
        fsm: OTAUpdateFSM,
        executor,
    ) -> bool:
        """
        Params:
            otaclient_get_exp: a callable that can retrieve otaclient last exp
        """
        _loop = asyncio.get_running_loop()
        return await _loop.run_in_executor(executor, fsm.stub_wait_for_local_update)

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

    _WAIT_FOR_SUBECUS_SWITCH_OTASTATUS = 30  # seconds

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

    async def _query_ecu(self, ecu_id: str, ecu_addr: str, ecu_port: int) -> ECUStatus:
        """Query ecu status API once."""
        try:
            resp = await OtaClientCall.status_call(
                ecu_id,
                ecu_addr,
                ecu_port,
                request=wrapper.StatusRequest(),
                timeout=self.subecu_query_timeout,
            )
        except ECUNoResponse as e:
            logger.debug(f"failed to query ECU's status: {e!r}")
            return self.ECUStatus.UNREACHABLE

        # if ANY OF the subecu and its child ecu are
        # not in SUCCESS/FAILURE otastatus, or unreacable, return False
        all_ready, all_success = True, True
        for ecu_id, ecu_result, ecu_status in resp.iter_ecu_status():
            if ecu_result != wrapper.FailureType.NO_FAILURE:
                all_ready = False
                break

            _status = ecu_status.status
            if _status in (wrapper.StatusOta.SUCCESS, wrapper.StatusOta.FAILURE):
                all_success &= _status == wrapper.StatusOta.SUCCESS
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

    async def ensure_tracked_ecu_ready(self) -> bool:
        """Loop pulling tracked ecus' status until are ready.

        Return:
            A bool to indicate if all subecus and its child are in SUCCESS status.

        NOTE: 1. ready means otastatus in SUCCESS or FAILURE,
              2. this method will block until all tracked ecus are ready,
              2. if subecu is unreachable, this tracker will still keep pulling.
        """
        # NOTE(20220914): It might be an edge condition that this tracker starts
        #                 faster than the subecus start their own update progress,
        #                 but the tracker is not able to tell the differences between
        #                 SUCCESS status before update starts, or SUCCESS status after
        #                 ota update applied.
        #
        #                 If the tracker receives unintended SUCCESS status from subecu,
        #                 it will finish tracking and returns immediately.
        #
        #                 To avoid this unintended behavior, we wait for subecus for
        #                 _WAIT_FOR_SUBECUS_SWITCH_OTASTATUS seconds to ensure subecus are in
        #                 UPDATING status before we start loop pulling the subecus' status.
        await asyncio.sleep(self._WAIT_FOR_SUBECUS_SWITCH_OTASTATUS)
        logger.info(f"start loop pulling subecus({self.tracked_ecus_dict=}) status...")
        while True:
            coros: List[Coroutine] = []
            for subecu_id, subecu_addr in self.tracked_ecus_dict.items():
                coros.append(
                    self._query_ecu(
                        subecu_id,
                        subecu_addr,
                        server_cfg.SERVER_PORT,
                    )
                )

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

        self._ecu_info = ECUInfo.parse_ecu_info(cfg.ECU_INFO_FILE)
        self.my_ecu_id = self._ecu_info.get_ecu_id()
        self.subecus_dict: Dict[str, str] = {
            ecu_id: ecu_ip_addr
            for ecu_id, ecu_ip_addr in self._ecu_info.iter_secondary_ecus()
        }

        # read ecu_info to get the booloader type,
        # if not specified, use detect_bootloader
        if (
            bootloader_type := self._ecu_info.get_bootloader()
        ) == BootloaderType.UNSPECIFIED:
            bootloader_type = detect_bootloader()
        # NOTE: inject bootloader and create_standby into otaclient
        self._ota_client = OTAClient(
            boot_control_cls=get_boot_controller(bootloader_type),
            create_standby_cls=get_standby_slot_creator(cfg.STANDBY_CREATION_MODE),
            my_ecu_id=self.my_ecu_id,
        )

        # for _get_subecu_status
        self._status_pulling_lock = asyncio.Lock()
        self._last_status_query = 0
        self._cached_status = wrapper.StatusResponse()

        # update session
        self._update_session = _RequestHandlingSession(
            enable_otaproxy=proxy_cfg.enable_local_ota_proxy,
            executor=self._executor,
        )

    ###### API stub method #######
    def host_addr(self):
        return self._ecu_info.get_ecu_ip_addr()

    async def update(self, request: wrapper.UpdateRequest) -> wrapper.UpdateResponse:
        logger.debug(f"receive update request: {request}")
        response = wrapper.UpdateResponse()

        if self._update_session.is_running:
            response.add_ecu(
                wrapper.UpdateResponseEcu(
                    ecu_id=self.my_ecu_id,
                    result=wrapper.FailureType.RECOVERABLE,
                )
            )
            logger.debug("ignore duplicated update request")
            return response
        else:
            # start this update session
            _init_cache = (
                self._ota_client.live_ota_status.get_ota_status()
                == wrapper.StatusOta.SUCCESS
            )
            await self._update_session.start(request, init_cache=_init_cache)

        # a list of directly connected ecus in the update request
        tracked_ecus_dict: Dict[str, str] = {}

        # simultaneously dispatching update requests to all subecus without blocking
        tasks: Dict[asyncio.Task, str] = {}
        for _ecu_id, _ecu_ip in self.subecus_dict.items():
            if not request.if_contains_ecu(_ecu_id):
                continue

            # add to tracked ecu list
            tracked_ecus_dict[_ecu_id] = _ecu_ip
            _task = asyncio.create_task(
                OtaClientCall.update_call(
                    _ecu_id,
                    _ecu_ip,
                    server_cfg.SERVER_PORT,
                    request=request,
                    timeout=server_cfg.WAITING_SUBECU_ACK_UPDATE_REQ_TIMEOUT,
                )
            )
            tasks[_task] = _ecu_id

        if tasks:  # NOTE: input for asyncio.wait must not be empty!
            done, _ = await asyncio.wait(tasks)
            for _task in done:
                try:
                    _ecu_resp: wrapper.UpdateResponse = _task.result()
                    response.merge_from(_ecu_resp)
                except ECUNoResponse as e:
                    ecu_id = tasks[_task]
                    logger.warning(
                        f"{tasks[_task]} doesn't respond to update request on-time"
                        f"(within {server_cfg.WAITING_SUBECU_ACK_UPDATE_REQ_TIMEOUT}s): {e!r}"
                    )
                    response.add_ecu(
                        wrapper.UpdateResponseEcu(
                            ecu_id=ecu_id,
                            result=wrapper.FailureType.RECOVERABLE,
                        )
                    )
            tasks.clear()

        # after all subecus ack the update request, update my ecu
        my_ecu_tracking_task: Optional[Coroutine] = None
        if entry := request.find_update_meta(self.my_ecu_id):
            if not self._ota_client.live_ota_status.request_update():
                logger.warning(
                    f"ota_client should not take ota update under ota_status={self._ota_client.live_ota_status.get_ota_status()}"
                )
                response.add_ecu(
                    wrapper.UpdateResponseEcu(
                        ecu_id=self.my_ecu_id,
                        result=wrapper.FailureType.RECOVERABLE,
                    )
                )
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
                my_ecu_tracking_task = _RequestHandlingSession.my_ecu_update_tracker(
                    executor=self._executor,
                    fsm=self._update_session.fsm,
                )

                response.add_ecu(
                    wrapper.UpdateResponseEcu(
                        ecu_id=self.my_ecu_id,
                        result=wrapper.FailureType.NO_FAILURE,
                    )
                )

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

    async def rollback(
        self, request: wrapper.RollbackRequest
    ) -> wrapper.RollbackResponse:
        logger.info(f"{request=}")
        response = wrapper.RollbackResponse()

        tracked_ecus_dict: Dict[str, str] = {}
        tasks: Dict[asyncio.Task, str] = {}
        for _ecu_id, _ecu_ip in self.subecus_dict.items():
            if not request.if_contains_ecu(_ecu_id):
                continue

            # add to tracked ecu list
            tracked_ecus_dict[_ecu_id] = _ecu_ip
            _task = asyncio.create_task(
                OtaClientCall.rollback_call(
                    _ecu_id,
                    _ecu_ip,
                    server_cfg.SERVER_PORT,
                    request=request,
                    timeout=server_cfg.WAITING_SUBECU_ACK_UPDATE_REQ_TIMEOUT,
                )
            )
            tasks[_task] = _ecu_id

        if tasks:  # NOTE: input for asyncio.wait must not be empty!
            done, _ = await asyncio.wait(tasks)
            for _task in done:
                try:
                    _ecu_resp: wrapper.RollbackResponse = _task.result()
                    response.merge_from(_ecu_resp)
                except ECUNoResponse as e:
                    ecu_id = tasks[_task]
                    logger.warning(
                        f"{tasks[_task]} doesn't respond to rollback request on-time"
                        f"(within {server_cfg.WAITING_SUBECU_ACK_UPDATE_REQ_TIMEOUT}s): {e!r}"
                    )
                    response.add_ecu(
                        wrapper.RollbackResponseEcu(
                            ecu_id=ecu_id,
                            result=wrapper.FailureType.RECOVERABLE,
                        )
                    )
            tasks.clear()
        # NOTE: we don't track ECU rollbacking currently
        logger.info(f"rollback: {tracked_ecus_dict=}")

        # after all subecus response the request, rollback my ecu
        if entry := request.if_contains_ecu(self.my_ecu_id):
            if self._ota_client.live_ota_status.request_rollback():
                logger.info(f"rollback my_ecu: {self.my_ecu_id=}, {entry=}")
                # dispatch the rollback request to threadpool
                self._executor.submit(self._ota_client.rollback)

                # rollback request is permitted, prepare the response
                response.add_ecu(
                    wrapper.RollbackResponseEcu(
                        ecu_id=self.my_ecu_id,
                        result=wrapper.FailureType.NO_FAILURE,
                    )
                )
            else:
                # current ota_client's status indicates that
                # the ota_client should not start an ota rollback
                logger.warning(
                    "ota_client should not take ota rollback under "
                    f"ota_status={self._ota_client.live_ota_status.get_ota_status()}"
                )
                response.add_ecu(
                    wrapper.RollbackResponseEcu(
                        ecu_id=self.my_ecu_id,
                        result=wrapper.FailureType.RECOVERABLE,
                    )
                )
        else:
            logger.warning("entries in rollback request doesn't include this ecu")

        return response

    async def status(self, _: Optional[StatusRequest] = None) -> StatusResponse:
        """
        NOTE: if the cached status is older than <server_cfg.QUERY_SUBECU_STATUS_INTERVAL>,
        the caller should take the responsibility to update the cached status.
        """
        cur_time = time.time()
        if cur_time - self._last_status_query > server_cfg.STATUS_UPDATE_INTERVAL:
            async with self._status_pulling_lock:
                self._last_status_query = cur_time
                resp = StatusResponse()

                # get subecus' status
                coros: List[Coroutine] = []
                for subecu_id, subecu_addr in self.subecus_dict.items():
                    coros.append(
                        OtaClientCall.status_call(
                            subecu_id,
                            subecu_addr,
                            server_cfg.SERVER_PORT,
                            request=wrapper.StatusRequest(),
                            timeout=server_cfg.QUERYING_SUBECU_STATUS_TIMEOUT,
                        )
                    )
                # merge the subecu and its child ecus status
                for subecu_resp in await asyncio.gather(*coros, return_exceptions=True):
                    if isinstance(subecu_resp, ECUNoResponse):
                        logger.debug(f"failed to query ECU's status: {subecu_resp!r}")
                        continue
                    resp.merge_from(subecu_resp)
                # prepare my_ecu status
                resp.add_ecu(self._ota_client.status())

                # register the status to cache
                resp.available_ecu_ids.extend(self._ecu_info.get_available_ecu_ids())
                self._cached_status = resp
        # response with cached status
        return self._cached_status
