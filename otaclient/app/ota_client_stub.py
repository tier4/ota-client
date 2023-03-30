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
import logging
import multiprocessing
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlsplit


from . import log_setting
from ._ecu_tracker import ChildECUTracker, PollingTask
from .boot_control import get_boot_controller
from .configs import server_cfg, config as cfg
from .common import ensure_http_server_open
from .create_standby import get_standby_slot_creator
from .ecu_info import ECUInfo
from .ota_client import OTAClient, OTAClientControlFlags
from .ota_client_call import OtaClientCall
from .proto import wrapper
from .proxy_info import proxy_cfg

from otaclient.ota_proxy.config import config as proxy_srv_cfg
from otaclient.ota_proxy import App, OTACache, OTACacheScrubHelper

logger = log_setting.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class OTAProxyLauncher:
    def __init__(self, *, executor: ThreadPoolExecutor) -> None:
        self._lock = asyncio.Lock()
        self.started = asyncio.Event()
        self.ready = asyncio.Event()
        # process start/shutdown will be dispatched to thread pool
        self._run_in_executor = partial(
            asyncio.get_event_loop().run_in_executor, executor
        )
        self.upper_proxy = (
            urlsplit(proxy_cfg.upper_ota_proxy) if proxy_cfg.upper_ota_proxy else None
        )
        self.local_otaproxy_enabled = proxy_cfg.enable_local_ota_proxy
        self._otaproxy = None

    @staticmethod
    def _start_otaproxy_process(init_cache: bool):
        """Main entry for ota_proxy in separate process."""
        import uvloop
        import uvicorn

        # ------ configure logging for ota_proxy in new process ------ #
        log_setting.configure_logging(
            loglevel=logging.CRITICAL, http_logging_url=log_setting.get_ecu_id()
        )
        otaproxy_logger = logging.getLogger("otaclient.ota_proxy")
        otaproxy_logger.setLevel(cfg.DEFAULT_LOG_LEVEL)

        # ------ scrub cache folder if cache re-use is possible ------ #
        cache_base_dir = Path(proxy_srv_cfg.BASE_DIR)
        cache_db_file = Path(proxy_srv_cfg.DB_FILE)
        should_init_cache = init_cache or not (
            cache_base_dir.is_dir() and cache_db_file.is_file()
        )
        if not should_init_cache:
            scrub_helper = OTACacheScrubHelper(cache_db_file, cache_base_dir)
            try:
                scrub_helper.scrub_cache()
            except Exception as e:
                logger.error(f"scrub cache failed, force init: {e!r}")
                should_init_cache = True
            finally:
                del scrub_helper

        # ------ wait for upper proxy active if any ------ #
        if upper_proxy := proxy_cfg.upper_ota_proxy:
            ensure_http_server_open(upper_proxy, interval=1, timeout=10 * 60)

        # ------ start otaproxy ------ #
        async def _start():
            ota_cache = OTACache(
                cache_enabled=proxy_cfg.enable_local_ota_proxy_cache,
                upper_proxy=proxy_cfg.upper_ota_proxy,
                enable_https=proxy_cfg.gateway,
                init_cache=init_cache,
            )

            # NOTE: explicitly set loop and http options
            #       to prevent using wrong libs
            # NOTE 2: explicitly set http="h11",
            #       as http=="httptools" breaks ota_proxy functionality
            config = uvicorn.Config(
                App(ota_cache),
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
        asyncio.run(_start())

    @property
    def is_started(self) -> bool:
        return self.started.is_set()

    async def start(self, *, init_cache: bool) -> Optional[int]:
        async with self._lock:
            if self.started.is_set():
                logger.warning("ignore multiple otaproxy start request")
                return
            self.started.set()

        # launch otaproxy server process
        mp_ctx = multiprocessing.get_context("spawn")
        otaproxy = mp_ctx.Process(
            target=self._start_otaproxy_process,
            args=(init_cache,),
            daemon=True,  # kill otaproxy if otaclient exists
        )
        await self._run_in_executor(otaproxy.start)
        self.ready.set()  # process started and ready
        self._otaproxy = otaproxy
        logger.info(
            f"otaproxy({otaproxy.pid=}) started at "
            f"{proxy_cfg.local_ota_proxy_listen_addr}:{proxy_cfg.local_ota_proxy_listen_port}"
        )
        return otaproxy.pid

    async def stop(self, *, cleanup_cache: bool):
        def _shutdown():
            if self._otaproxy:
                self._otaproxy.terminate()
                self._otaproxy.join()
            self._otaproxy = None

            if cleanup_cache:
                logger.info("cleanup ota_cache on success")
                shutil.rmtree(proxy_srv_cfg.BASE_DIR, ignore_errors=True)

        async with self._lock:
            if self.started.is_set() and self.ready.is_set():
                await self._run_in_executor(_shutdown)
            self.ready.clear()
            self.started.clear()
            logger.info("otaproxy closed")


class OTAClientBusy(Exception):
    """Raised when otaclient receive another request when doing update/rollback."""


class OTAClientStub:
    STATUS_POLLING_INTERVAL = cfg.STATS_COLLECT_INTERVAL

    def __init__(
        self,
        *,
        ecu_info: ECUInfo,
        executor: ThreadPoolExecutor,
        control_flags: OTAClientControlFlags,
    ) -> None:
        self.update_rollback_lock = (
            asyncio.Lock()
        )  # only one update/rollback is allowed at a time
        self.my_ecu_id = ecu_info.ecu_id
        self._run_in_executor = partial(
            asyncio.get_running_loop().run_in_executor, executor
        )
        self._control_flags = control_flags

        self.otaclient = OTAClient(
            boot_control_cls=get_boot_controller(ecu_info.get_bootloader()),
            create_standby_cls=get_standby_slot_creator(cfg.STANDBY_CREATION_MODE),
            my_ecu_id=self.my_ecu_id,
        )

        # proxy used by local otaclient
        # NOTE: it can be an upper proxy, or local otaproxy
        self.local_used_proxy_url = (
            urlsplit(_proxy)
            if (_proxy := proxy_cfg.get_proxy_for_local_ota())
            else None
        )
        self.local_otaproxy_enabled = proxy_cfg.enable_local_ota_proxy

        self.shutdown_event = asyncio.Event()
        self.last_status_report = self.otaclient.status()
        self.last_status_report_timestamp = 0
        self.last_operation = None  # update/rollback/None

        self._status_polling_task = PollingTask(
            poll_interval=self.STATUS_POLLING_INTERVAL
        )
        self._status_polling_task = self._status_polling_task.start(
            self._status_polling
        )

    async def _status_polling(self):
        status_report = await self._run_in_executor(self.otaclient.status)
        self.last_status_report = status_report
        self.last_status_report_timestamp = int(time.time())

    # properties

    @property
    def requires_local_otaproxy(self) -> bool:
        if not self.local_otaproxy_enabled:
            return False
        return self.last_status_report.is_updating_and_requires_network

    @property
    def is_updating(self) -> bool:
        return self.last_status_report.is_in_update

    @property
    def is_failed(self) -> bool:
        return self.last_status_report.is_failed

    @property
    def is_success(self) -> bool:
        return self.last_status_report.is_success

    # API method

    async def dispatch_update(self, request: wrapper.UpdateRequestEcu):
        """Dispatch update request to otaclient.

        Raises:
            OTAClientBusy if otaclient is already executing update/rollback.
        """
        if self.update_rollback_lock.locked():
            raise OTAClientBusy(f"ongoing operation: {self.last_operation=}")

        async def _update():
            async with self.update_rollback_lock:
                self.last_operation = wrapper.StatusOta.UPDATING
                try:
                    await self._run_in_executor(
                        partial(
                            self.otaclient.update,
                            request.version,
                            request.url,
                            request.cookies,
                            control_flags=self._control_flags,
                        )
                    )
                finally:
                    self.last_operation = None

        # dispatch update to background
        asyncio.create_task(_update())

    async def dispatch_rollback(self, _: wrapper.RollbackRequestEcu):
        """Dispatch update request to otaclient.

        Raises:
            OTAClientBusy if otaclient is already executing update/rollback.
        """
        if self.update_rollback_lock.locked():
            raise OTAClientBusy(f"ongoing operation: {self.last_operation=}")

        async def _rollback():
            async with self.update_rollback_lock:
                self.last_operation = wrapper.StatusOta.ROLLBACKING
                try:
                    await self._run_in_executor(self.otaclient.rollback)
                finally:
                    self.last_operation = None

        # dispatch to background
        asyncio.create_task(_rollback())

    def get_status(self) -> wrapper.StatusResponseEcuV2:
        return self.last_status_report


class _OTAUpdateRequestHandler:
    """
    Implementation of logics when an update request is received.

    The following logics are implemented:
    1. otaclient should not reboot when there is at least one child
        ECU or self ECU still requires local otaproxy,
    2. start otaproxy when at least one child ECU or self ECU requires
        local otaproxy,
    3. shutdown otaproxy if running when no ECU requires it.
    """

    DELAY = 60  # seconds
    POLLING_INTERVAL = 10  # seconds
    NORMAL_STATUS_POLL_INTERVAL = 30  # seconds
    ACTIVE_STATUS_POLL_INTERVAL = 1  # seconds

    def __init__(
        self,
        *,
        otaclient_stub: OTAClientStub,
        ecu_tracker: ChildECUTracker,
        otaproxy: OTAProxyLauncher,
        control_flag: OTAClientControlFlags,
    ) -> None:
        self.shutdown_event = asyncio.Event()
        self.active_update_exist = asyncio.Event()
        self.last_update_request_received_timestamp = 0

        self._otaproxy = otaproxy
        self._otaclient_stub = otaclient_stub
        self._ecu_tracker = ecu_tracker
        self._control_flag = control_flag

    # internal properties

    @property
    def _any_failed(self) -> bool:
        _, any_failed_child_ecu = self._ecu_tracker.any_failed_ecu
        return self._otaclient_stub.is_failed or any_failed_child_ecu

    @property
    def _any_requires_otaproxy(self) -> bool:
        """if any of the child ECU or self ECU requires self ECU's otaproxy."""
        (
            _,
            any_child_ecu_requires_otaproxy,
        ) = self._ecu_tracker.any_requires_otaproxy
        return (
            self._otaclient_stub.requires_local_otaproxy
            and any_child_ecu_requires_otaproxy
        )

    # internal methods

    async def _start_otaproxy(self):
        if self._otaproxy.is_started:
            return
        await self._otaproxy.start(init_cache=not self._any_failed)
        self._control_flag

    async def _stop_otaproxy(self):
        if not self._otaproxy.is_started:
            return
        # TODO: only cleanup cache for SUCCESS ECU.
        await self._otaproxy.stop(cleanup_cache=not self._any_failed)

    async def _status_checking(self):
        while not self.shutdown_event.is_set():
            cur_timestamp = int(time.time())

            # NOTE: introduce DELAY to prevent pre-mature otaproxy stopping due to
            #       some of the child ECUs not yet received/react to update request
            not_pre_mature_status_switching = (
                self.last_update_request_received_timestamp + self.DELAY > cur_timestamp
            )

            # otaproxy lifecycle/dependency management
            if self._any_requires_otaproxy:
                if not self._otaproxy.is_started:
                    await self._otaproxy.start(init_cache=not self._any_failed)
                    self._control_flag.clear_can_reboot_flag()
            else:
                if self._otaproxy.is_started and not_pre_mature_status_switching:
                    await self._otaproxy.stop(cleanup_cache=not self._any_failed)
                    self._control_flag.set_can_reboot_flag()

            # status polling interval tunning if no active update
            if not (
                self._ecu_tracker.any_in_update or self._otaclient_stub.is_updating
            ):
                if (
                    self.active_update_exist.is_set()
                    and not_pre_mature_status_switching
                ):
                    self.active_update_exist.clear()
                    self._ecu_tracker.set_polling_interval(
                        self.NORMAL_STATUS_POLL_INTERVAL
                    )

            await asyncio.sleep(self.POLLING_INTERVAL)

    # API

    async def on_update_request(self, _: wrapper.UpdateRequest):
        """A fast-path to trigger active status polling."""
        self.last_update_request_received_timestamp = int(time.time())
        self.active_update_exist.set()
        # reduce status polling task interval on active ECU cluster
        self._ecu_tracker.set_polling_interval(self.ACTIVE_STATUS_POLL_INTERVAL)


class OTAClientServiceStub:
    NORMAL_STATUS_POLL_INTERVAL = 30  # seconds
    ACTIVE_STATUS_POLL_INTERVAL = 1  # seconds

    def __init__(self):
        self._executor = ThreadPoolExecutor(thread_name_prefix="otaclient_service_stub")
        self._run_in_executor = partial(
            asyncio.get_running_loop().run_in_executor, self._executor
        )

        ecu_info = ECUInfo.parse_ecu_info(cfg.ECU_INFO_FILE)
        self.ecu_info = ecu_info
        self.my_ecu_id = ecu_info.get_ecu_id()

        self._otaclient_control_flags = OTAClientControlFlags()
        self._otaclient_stub = OTAClientStub(
            ecu_info=ecu_info,
            executor=self._executor,
            control_flags=self._otaclient_control_flags,
        )
        self._otaproxy = OTAProxyLauncher(executor=self._executor)
        self._ecu_tracker = ChildECUTracker(ecu_info)

        # update request handler
        self._update_request_handler = _OTAUpdateRequestHandler(
            otaclient_stub=self._otaclient_stub,
            otaproxy=self._otaproxy,
            ecu_tracker=self._ecu_tracker,
            control_flag=self._otaclient_control_flags,
        )

    # API stub

    async def update(self, request: wrapper.UpdateRequest) -> wrapper.UpdateResponse:
        logger.info(f"receive update request: {request}")
        # signal the update handler
        await self._update_request_handler.on_update_request(request)
        response = wrapper.UpdateResponse()

        # first: dispatch update request to all directly connected subECUs
        # simultaneously dispatching update requests to all subecus without blocking
        tasks: List[asyncio.Task] = []
        for ecu_contact in self.ecu_info.iter_direct_subecu_contact():
            if request.if_contains_ecu(ecu_contact.ecu_id):
                logger.debug(f"send update request to ecu@{ecu_contact=}")
                tasks.append(
                    asyncio.create_task(
                        OtaClientCall.update_call(
                            ecu_contact.ecu_id,
                            ecu_contact.host,
                            ecu_contact.port,
                            request=request,
                            timeout=server_cfg.WAITING_SUBECU_ACK_REQ_TIMEOUT,
                        )
                    )
                )
        for _task in asyncio.as_completed(*tasks):
            _resp: wrapper.UpdateResponse = _task.result()
            response.merge_from(_resp)

        # second: dispatch update request to local if required
        if update_req_ecu := request.find_update_meta(self.my_ecu_id):
            _resp_ecu = wrapper.UpdateResponseEcu(ecu_id=self.my_ecu_id)
            try:
                await self._otaclient_stub.dispatch_update(update_req_ecu)
            except OTAClientBusy as e:
                logger.error(f"self ECU is busy: {e!r}")
                _resp_ecu.result = wrapper.FailureType.RECOVERABLE
            response.add_ecu(_resp_ecu)

        return response

    async def rollback(
        self, request: wrapper.RollbackRequest
    ) -> wrapper.RollbackResponse:
        logger.info(f"receive rollback request: {request}")
        response = wrapper.RollbackResponse()

        # first: dispatch rollback request to all directly connected subECUs
        tasks: List[asyncio.Task] = []
        for ecu_contact in self.ecu_info.iter_direct_subecu_contact():
            if request.if_contains_ecu(ecu_contact.ecu_id):
                logger.debug(f"send rollback request to ecu@{ecu_contact=}")
                tasks.append(
                    asyncio.create_task(
                        OtaClientCall.rollback_call(
                            ecu_contact.ecu_id,
                            ecu_contact.host,
                            ecu_contact.port,
                            request=request,
                            timeout=server_cfg.WAITING_SUBECU_ACK_REQ_TIMEOUT,
                        )
                    )
                )
        for _task in asyncio.as_completed(*tasks):
            _resp: wrapper.RollbackResponse = _task.result()
            response.merge_from(_resp)

        # second: dispatch rollback request to local if required
        if rollback_req := request.find_rollback_req(self.my_ecu_id):
            _resp_ecu = wrapper.RollbackResponseEcu(ecu_id=self.my_ecu_id)
            try:
                await self._otaclient_stub.dispatch_rollback(rollback_req)
            except OTAClientBusy as e:
                logger.error(f"self ECU is busy: {e!r}")
                _resp_ecu.result = wrapper.FailureType.RECOVERABLE
            response.add_ecu(_resp_ecu)

        return response

    async def status(
        self, _: Optional[wrapper.StatusRequest] = None
    ) -> wrapper.StatusResponse:
        res = wrapper.StatusResponse()
        # populate available_ecu_ids
        available_ecu_ids = set()
        available_ecu_ids.update(
            self.ecu_info.get_available_ecu_ids(),
            self._ecu_tracker.get_available_child_ecu_ids(),
        )
        res.available_ecu_ids.extend(available_ecu_ids)
        # populate status_resp_ecu
        for ecu_status in self._ecu_tracker.get_status_list():
            res.add_ecu(ecu_status)
        res.add_ecu(self._otaclient_stub.get_status())
        return res
