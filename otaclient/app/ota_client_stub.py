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
from typing import Optional
from urllib.parse import urlsplit


from . import log_setting
from ._ecu_tracker import ECUStatusStorage, SubECUTracker
from .configs import config as cfg
from .common import ensure_http_server_open
from .ecu_info import ECUInfo
from .ota_client import OTAClientBusy, OTAClientControlFlags, OTAClientStub
from .ota_client_call import batch_rollback, batch_update
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
    def is_running(self) -> bool:
        return self.started.is_set() and self.ready.is_set()

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
        if self._lock.locked():
            return

        def _shutdown():
            if self._otaproxy:
                self._otaproxy.terminate()
                self._otaproxy.join()
            self._otaproxy = None

            if cleanup_cache:
                logger.info("cleanup ota_cache on success")
                shutil.rmtree(proxy_srv_cfg.BASE_DIR, ignore_errors=True)

        async with self._lock:
            self.ready.clear()
            self.started.clear()
            if self.started.is_set() and self.ready.is_set():
                await self._run_in_executor(_shutdown)
            logger.info("otaproxy closed")


class OTAClientServiceStub:
    DELAY = 60  # seconds
    POLLING_INTERVAL = 20  # seconds
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

        # a storage that contains all child ECUs and seld ECU status
        self._ecu_status_storage = ECUStatusStorage()
        # tracker that keeps tracking all child ECUs status
        self._sub_ecu_tracker = SubECUTracker(
            ecu_info, storage=self._ecu_status_storage
        )
        self._otaclient_stub = OTAClientStub(
            ecu_info=ecu_info,
            executor=self._executor,
            control_flags=self._otaclient_control_flags,
            ecu_status_storage=self._ecu_status_storage,
        )
        self._otaproxy_launcher = OTAProxyLauncher(executor=self._executor)

        # status tracker
        self._status_checking_shutdown_event = asyncio.Event()
        self._status_tracker = asyncio.create_task(self._status_checking())

    # internal, status checking loop

    def _is_within_delay_period(self):
        """OTAProxy should not be altered within the delay period."""
        cur_timestamp = int(time.time())
        return cur_timestamp <= self.last_update_request_received_timestamp + self.DELAY

    async def _otaproxy_lifecycle_management(self):
        if self._otaproxy_launcher.is_running:
            if not self._ecu_status_storage.any_requires_network:
                no_failed = not self._ecu_status_storage.any_failed
                await self._otaproxy_launcher.stop(cleanup_cache=no_failed)
        else:
            no_failed = not self._ecu_status_storage.any_failed
            await self._otaproxy_launcher.start(init_cache=no_failed)

    async def _status_checking(self):
        while not self._status_checking_shutdown_event.is_set():
            # otaproxy management
            if not self._is_within_delay_period():
                await self._otaproxy_lifecycle_management()

            # otaclient control flag
            if not self._ecu_status_storage.any_requires_network:
                self._otaclient_control_flags.set_can_reboot_flag()

    async def _on_update_request(self, _: wrapper.UpdateRequest):
        """A fast-path to trigger active status polling."""
        self.last_update_request_received_timestamp = int(time.time())

    # API stub

    async def update(self, request: wrapper.UpdateRequest) -> wrapper.UpdateResponse:
        logger.info(f"receive update request: {request}")
        # signal the update handler
        await self._on_update_request(request)
        response = wrapper.UpdateResponse()

        # first: dispatch update request to all directly connected subECUs
        # simultaneously dispatching update requests to all subecus without blocking
        for _resp in batch_update(self.ecu_info, request):
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
        for _resp in batch_rollback(self.ecu_info, request):
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
        return self._ecu_status_storage.export()
