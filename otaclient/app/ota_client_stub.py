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
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from itertools import chain
from typing import Optional, Set, Dict

from . import log_setting
from .configs import config as cfg, server_cfg
from .common import ensure_http_server_open
from .ecu_info import ECUContact, ECUInfo
from .ota_client import OTAClientBusy, OTAClientControlFlags, OTAClientStub
from .ota_client_call import OtaClientCall, batch_rollback, batch_update
from .proto import wrapper
from .proxy_info import proxy_cfg

from otaclient.ota_proxy.config import config as local_otaproxy_cfg
from otaclient.ota_proxy import subprocess_start_otaproxy


logger = log_setting.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class OTAProxyLauncher:
    def __init__(self, *, executor: ThreadPoolExecutor) -> None:
        self.upper_otaproxy = proxy_cfg.upper_ota_proxy

        self._lock = asyncio.Lock()
        self.started = asyncio.Event()
        self.ready = asyncio.Event()
        # process start/shutdown will be dispatched to thread pool
        self._run_in_executor = partial(
            asyncio.get_event_loop().run_in_executor, executor
        )
        self._otaproxy_subprocess = None

    @property
    def is_running(self) -> bool:
        return self.started.is_set() and self.ready.is_set()

    @staticmethod
    def _subprocess_init(upper_proxy: Optional[str] = None):
        """Initializing the subprocess before launching it.

        Currently only used for configuring the logging for otaproxy.
        """
        # configure logging for otaproxy subprocess
        log_setting.configure_logging(
            loglevel=logging.CRITICAL, http_logging_url=log_setting.get_ecu_id()
        )
        otaproxy_logger = logging.getLogger("otaclient.ota_proxy")
        otaproxy_logger.setLevel(cfg.DEFAULT_LOG_LEVEL)

        # wait for upper otaproxy if any
        if upper_proxy:
            ensure_http_server_open(upper_proxy)

    async def start(self, *, init_cache: bool) -> Optional[int]:
        async with self._lock:
            if self.started.is_set():
                logger.warning("ignore multiple otaproxy start request")
                return
            self.started.set()

        # launch otaproxy server process
        otaproxy_subprocess = await self._run_in_executor(
            partial(
                subprocess_start_otaproxy,
                host=proxy_cfg.local_ota_proxy_listen_addr,
                port=proxy_cfg.local_ota_proxy_listen_port,
                init_cache=init_cache,
                cache_dir=local_otaproxy_cfg.BASE_DIR,
                cache_db_f=local_otaproxy_cfg.DB_FILE,
                upper_proxy=proxy_cfg.upper_ota_proxy,
                enable_cache=proxy_cfg.enable_local_ota_proxy_cache,
                enable_https=proxy_cfg.gateway,
                subprocess_init=partial(self._subprocess_init, self.upper_otaproxy),
            )
        )
        self.ready.set()  # process started and ready
        self._otaproxy_subprocess = otaproxy_subprocess
        logger.info(
            f"otaproxy({otaproxy_subprocess.pid=}) started at "
            f"{proxy_cfg.local_ota_proxy_listen_addr}:{proxy_cfg.local_ota_proxy_listen_port}"
        )
        return otaproxy_subprocess.pid

    async def stop(self, *, cleanup_cache: bool):
        if self._lock.locked():
            return

        def _shutdown():
            if self._otaproxy_subprocess:
                self._otaproxy_subprocess.terminate()
                self._otaproxy_subprocess.join()
            self._otaproxy_subprocess = None

            if cleanup_cache:
                logger.info("cleanup ota_cache on success")
                shutil.rmtree(local_otaproxy_cfg.BASE_DIR, ignore_errors=True)

        async with self._lock:
            self.ready.clear()
            self.started.clear()
            await self._run_in_executor(_shutdown)
            logger.info("otaproxy closed")


class ECUStatusStorage:
    UNREACHABLE_ECU_TIMEOUT = cfg.UNREACHABLE_ECU_TIMEOUT
    PROPERTY_REFRESH_INTERVAL = 6

    def __init__(self) -> None:
        self._writer_lock = asyncio.Lock()
        # ECU status storage
        self.storage_last_updated_timestamp = 0
        self._all_available_ecus_id: Set[str] = set()
        self._all_ecus_status_v2: Dict[str, wrapper.StatusResponseEcuV2] = {}
        self._all_ecus_status_v1: Dict[str, wrapper.StatusResponseEcu] = {}
        self._all_ecus_last_contact_timestamp: Dict[str, int] = {}

        # properties cache
        self.lost_ecus_id = set()
        self.any_in_update = False
        self.any_failed = False
        self.any_requires_network = False

        # property update task
        self.properties_update_shutdown_event = asyncio.Event()
        self._properties_update_task = asyncio.create_task(
            self._loop_updating_properties()
        )

    def _is_ecu_lost(self, ecu_id: str, cur_timestamp: int) -> bool:
        return (
            cur_timestamp
            > self._all_ecus_last_contact_timestamp[ecu_id]
            + self.UNREACHABLE_ECU_TIMEOUT
        )

    async def _properties_update(self):
        cur_timestamp = int(time.time())

        # update lost ecu list
        lost_ecus = set()
        for ecu_id in self._all_available_ecus_id:
            if self._is_ecu_lost(ecu_id, cur_timestamp):
                lost_ecus.add(ecu_id)
        # add ECUs that never appear
        lost_ecus.add(self._all_available_ecus_id - set(self._all_ecus_status_v2))
        self.lost_ecus_id = list(lost_ecus)

        self.any_in_update = any(
            (
                status.is_in_update
                for status in chain(
                    self._all_ecus_status_v2.values(), self._all_ecus_status_v1.values()
                )
                if status.ecu_id not in lost_ecus
            )
        )
        self.any_failed = any(
            (
                status.is_failed
                for status in chain(
                    self._all_ecus_status_v2.values(), self._all_ecus_status_v1.values()
                )
                if status.ecu_id not in lost_ecus
            )
        )
        self.any_requires_network = any(
            (
                status.if_requires_network
                for status in chain(
                    self._all_ecus_status_v2.values(), self._all_ecus_status_v1.values()
                )
                if status.ecu_id not in lost_ecus
            )
        )
        self.storage_last_updated_timestamp = cur_timestamp

    async def _loop_updating_properties(self):
        last_timestamp = self.storage_last_updated_timestamp
        while not self.properties_update_shutdown_event.is_set():
            # only update properties when storage is updated
            if last_timestamp != self.storage_last_updated_timestamp:
                last_timestamp = self.storage_last_updated_timestamp
                await self._properties_update()
            await asyncio.sleep(self.PROPERTY_REFRESH_INTERVAL)

    # API

    async def update_from_child_ECU(self, status_resp: wrapper.StatusResponse):
        """SubECUTracker calls this method to update storage with subECUs' status report."""
        async with self._writer_lock:
            cur_timestamp = int(time.time())
            self.storage_last_updated_timestamp = cur_timestamp
            self._all_available_ecus_id.update(status_resp.available_ecu_ids)

            # NOTE: explicitly support v1 format
            for ecu_status_v2 in status_resp.ecu_v2:
                ecu_id = ecu_status_v2.ecu_id
                self._all_ecus_status_v2[ecu_id] = ecu_status_v2
                self._all_ecus_last_contact_timestamp[ecu_id] = cur_timestamp
            for ecu_status_v1 in status_resp.ecu:
                ecu_id = ecu_status_v1.ecu_id
                self._all_ecus_status_v1[ecu_id] = ecu_status_v1
                self._all_ecus_last_contact_timestamp[ecu_id] = cur_timestamp

    async def update_from_local_ECU(self, ecu_status: wrapper.StatusResponseEcuV2):
        """OTAClientStub calls this method to update storage with local ECU's status report."""
        async with self._writer_lock:
            cur_timestamp = int(time.time())
            self.storage_last_updated_timestamp = cur_timestamp
            ecu_id = ecu_status.ecu_id
            self._all_ecus_status_v2[ecu_id] = ecu_status
            self._all_ecus_last_contact_timestamp[ecu_id] = cur_timestamp

    def export(self) -> wrapper.StatusResponse:
        res = wrapper.StatusResponse()
        res.available_ecu_ids.extend(self._all_available_ecus_id)

        ecu_using_v2 = set()
        for ecu_id, ecu_status_v2 in self._all_ecus_status_v2.items():
            res.ecu_v2.append(ecu_status_v2)
            ecu_using_v2.add(ecu_id)

        for ecu_id, ecu_status_v1 in self._all_ecus_status_v1.items():
            if ecu_id in ecu_using_v2:
                continue  # if v2 is used, skip
            res.ecu.append(ecu_status_v1)
        return res


class SubECUTracker:
    """Loop polling ECU status from directly connected ECUs."""

    IDLE_POLLING_INTERVAL = cfg.ACTIVE_INTERVAL
    ACTIVE_POLLING_INTERVAL = cfg.ACTIVE_INTERVAL

    def __init__(self, ecu_info: ECUInfo, storage: ECUStatusStorage) -> None:
        self._direct_subecu = {
            _ecu.ecu_id: _ecu for _ecu in ecu_info.iter_direct_subecu_contact()
        }
        self._storage = storage

        self.polling_shutdown_event = asyncio.Event()
        self._polling_task = asyncio.create_task(self._polling())

    async def _polling(self):
        while not self.polling_shutdown_event.is_set():
            await self._poll_direct_subECU_once()
            await asyncio.sleep(
                self.ACTIVE_POLLING_INTERVAL
                if self._storage.any_in_update
                else self.IDLE_POLLING_INTERVAL
            )

    async def _poll_direct_subECU_once(self):
        poll_tasks: Dict[asyncio.Future, ECUContact] = {}
        for _, ecu_contact in self._direct_subecu.items():
            _task = asyncio.create_task(
                OtaClientCall.status_call(
                    ecu_contact.ecu_id,
                    ecu_contact.host,
                    ecu_contact.port,
                    timeout=server_cfg.SERVER_PORT,
                )
            )
            poll_tasks[_task] = ecu_contact

        _fut: asyncio.Future
        for _fut in asyncio.as_completed(*poll_tasks):
            ecu_contact = poll_tasks[_fut]
            try:
                subecu_resp: wrapper.StatusResponse = _fut.result()
                assert subecu_resp
            except Exception as e:
                logger.debug(f"failed to contact ecu@{ecu_contact=}: {e!r}")
                continue
            await self._storage.update_from_child_ECU(subecu_resp)
        poll_tasks.clear()


class OTAClientServiceStub:
    IDLE_POLLING_INTERVAL = cfg.ACTIVE_INTERVAL
    ACTIVE_POLLING_INTERVAL = cfg.ACTIVE_INTERVAL

    def __init__(self):
        self._executor = ThreadPoolExecutor(thread_name_prefix="otaclient_service_stub")
        self._run_in_executor = partial(
            asyncio.get_running_loop().run_in_executor, self._executor
        )

        ecu_info = ECUInfo.parse_ecu_info(cfg.ECU_INFO_FILE)
        self.ecu_info = ecu_info
        self.listen_addr = ecu_info.ip_addr
        self.listen_port = server_cfg.SERVER_PORT
        self.my_ecu_id = ecu_info.get_ecu_id()

        self._ecu_status_storage = ECUStatusStorage()
        # tracker that keeps tracking all child ECUs status
        self._sub_ecu_tracker = SubECUTracker(
            ecu_info, storage=self._ecu_status_storage
        )
        self._otaclient_control_flags = OTAClientControlFlags()
        self._otaclient_stub = OTAClientStub(
            ecu_info=ecu_info,
            executor=self._executor,
            control_flags=self._otaclient_control_flags,
        )
        self._otaproxy_launcher = OTAProxyLauncher(executor=self._executor)

        # status tracker
        self._status_checking_shutdown_event = asyncio.Event()
        self._status_tracker = asyncio.create_task(self._status_checking())
        # local ecu status tracker
        self._local_ecu_status_checking_shutdown_event = asyncio.Event()
        self._local_ecu_status_tracking_task = asyncio.create_task(
            self._local_ecu_status_polling()
        )

    # internal, status checking loop

    async def _local_ecu_status_polling(self):
        while not self._local_ecu_status_checking_shutdown_event.is_set():
            status_report = await self._run_in_executor(self._otaclient_stub.get_status)
            await self._ecu_status_storage.update_from_local_ECU(status_report)
            # polling actively when otaclient is actively updating/rollbacking
            await asyncio.sleep(
                self.ACTIVE_POLLING_INTERVAL
                if self._otaclient_stub.is_busy
                else self.IDLE_POLLING_INTERVAL
            )

    async def _otaproxy_lifecycle_management(self):
        if self._otaproxy_launcher.is_running:
            if not self._ecu_status_storage.any_requires_network:
                no_failed = not self._ecu_status_storage.any_failed
                await self._otaproxy_launcher.stop(cleanup_cache=no_failed)
        else:
            if self._ecu_status_storage.any_requires_network:
                no_failed = not self._ecu_status_storage.any_failed
                await self._otaproxy_launcher.start(init_cache=no_failed)

    async def _status_checking(self):
        while not self._status_checking_shutdown_event.is_set():
            # otaproxy management
            await self._otaproxy_lifecycle_management()
            # otaclient control flag
            if not self._ecu_status_storage.any_requires_network:
                self._otaclient_control_flags.set_can_reboot_flag()

            await asyncio.sleep(
                self.ACTIVE_POLLING_INTERVAL
                if self._ecu_status_storage.any_in_update
                else self.IDLE_POLLING_INTERVAL
            )

    # API stub

    async def update(self, request: wrapper.UpdateRequest) -> wrapper.UpdateResponse:
        logger.info(f"receive update request: {request}")
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
