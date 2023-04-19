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
from pathlib import Path
from typing import Optional, Set, Dict

from . import log_setting
from .configs import config as cfg, server_cfg
from .common import ensure_otaproxy_start
from .ecu_info import ECUContact, ECUInfo
from .ota_client import OTAClientBusy, OTAClientControlFlags, OTAClientStub
from .ota_client_call import ECU_NO_RESPONSE, OtaClientCall
from .proto import wrapper
from .proxy_info import proxy_cfg

from otaclient.ota_proxy.config import config as local_otaproxy_cfg
from otaclient.ota_proxy import subprocess_start_otaproxy


logger = log_setting.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class OTAProxyLauncher:
    def __init__(
        self,
        *,
        executor: ThreadPoolExecutor,
        _proxy_info=proxy_cfg,
        _proxy_server_cfg=local_otaproxy_cfg,
    ) -> None:
        self._proxy_info = _proxy_info
        self._proxy_server_cfg = _proxy_server_cfg
        self.enabled = _proxy_info.enable_local_ota_proxy
        self.upper_otaproxy = _proxy_info.upper_ota_proxy

        self._lock = asyncio.Lock()
        # process start/shutdown will be dispatched to thread pool
        self._run_in_executor = partial(
            asyncio.get_event_loop().run_in_executor, executor
        )
        self._otaproxy_subprocess = None

    @property
    def is_running(self) -> bool:
        return (
            self.enabled
            and self._otaproxy_subprocess is not None
            and self._otaproxy_subprocess.is_alive()
        )

    def cleanup_cache_dir(self):
        if (cache_dir := Path(self._proxy_server_cfg.BASE_DIR)).is_dir():
            logger.info("cleanup ota_cache on success")
            shutil.rmtree(cache_dir, ignore_errors=True)

    @staticmethod
    def _subprocess_init(upper_proxy: Optional[str] = None):
        """Initializing the subprocess before launching it.

        Currently only used for configuring the logging for otaproxy.
        """
        # configure logging for otaproxy subprocess
        # NOTE: on otaproxy subprocess, we first set log level of the root logger
        #       to CRITICAL to filter out third_party libs' logging(requests, urllib3, etc.),
        #       and then set the otaclient.ota_proxy logger to DEFAULT_LOG_LEVEL
        log_setting.configure_logging(
            loglevel=logging.CRITICAL, http_logging_url=log_setting.get_ecu_id()
        )
        otaproxy_logger = logging.getLogger("otaclient.ota_proxy")
        otaproxy_logger.setLevel(cfg.DEFAULT_LOG_LEVEL)

        # wait for upper otaproxy if any
        if upper_proxy:
            ensure_otaproxy_start(upper_proxy)

    async def start(self, *, init_cache: bool) -> Optional[int]:
        if not self.enabled or self._lock.locked() or self.is_running:
            return
        async with self._lock:
            # launch otaproxy server process
            otaproxy_subprocess = await self._run_in_executor(
                partial(
                    subprocess_start_otaproxy,
                    host=self._proxy_info.local_ota_proxy_listen_addr,
                    port=self._proxy_info.local_ota_proxy_listen_port,
                    init_cache=init_cache,
                    cache_dir=self._proxy_server_cfg.BASE_DIR,
                    cache_db_f=self._proxy_server_cfg.DB_FILE,
                    upper_proxy=self.upper_otaproxy,
                    enable_cache=self._proxy_info.enable_local_ota_proxy_cache,
                    enable_https=self._proxy_info.gateway,
                    subprocess_init=partial(self._subprocess_init, self.upper_otaproxy),
                )
            )
            self._otaproxy_subprocess = otaproxy_subprocess
            logger.info(
                f"otaproxy({otaproxy_subprocess.pid=}) started at "
                f"{proxy_cfg.local_ota_proxy_listen_addr}:{proxy_cfg.local_ota_proxy_listen_port}"
            )
            return otaproxy_subprocess.pid

    async def stop(self):
        if not self.enabled or self._lock.locked() or not self.is_running:
            return

        def _shutdown():
            if self._otaproxy_subprocess and self._otaproxy_subprocess.is_alive():
                self._otaproxy_subprocess.terminate()
                self._otaproxy_subprocess.join()
            self._otaproxy_subprocess = None

        async with self._lock:
            await self._run_in_executor(_shutdown)
            logger.info("otaproxy closed")


class ECUStatusStorage:
    DELAY_PROPERTY_UPDATE = cfg.ON_RECEIVE_UPDATE_DELAY_ECU_STORAGE_PROPERTIES_UPDATE
    UNREACHABLE_ECU_TIMEOUT = cfg.UNREACHABLE_ECU_TIMEOUT
    PROPERTY_REFRESH_INTERVAL = 6
    IDLE_POLLING_INTERVAL = cfg.IDLE_INTERVAL
    ACTIVE_POLLING_INTERVAL = cfg.ACTIVE_INTERVAL

    def __init__(self) -> None:
        self._writer_lock = asyncio.Lock()
        # ECU status storage
        self.storage_last_updated_timestamp = 0
        self._all_available_ecus_id: Set[str] = set()
        self._all_ecus_status_v2: Dict[str, wrapper.StatusResponseEcuV2] = {}
        self._all_ecus_status_v1: Dict[str, wrapper.StatusResponseEcu] = {}
        self._all_ecus_last_contact_timestamp: Dict[str, int] = {}

        # properties cache
        self._properties_update_lock = asyncio.Lock()
        self.properties_last_update_timestamp = 0
        self.lost_ecus_id = set()
        self.updating_ecus = set()
        self.any_in_update = False
        self.failed_ecus = set()
        self.any_failed = False
        self.any_requires_network = False
        self.success_ecus = set()
        self.all_success = False

        # property update task
        self.properties_update_shutdown_event = asyncio.Event()
        asyncio.create_task(self._loop_updating_properties())

        # on receive update request
        self.last_update_request_received_timestamp = 0

    def _is_ecu_lost(self, ecu_id: str, cur_timestamp: int) -> bool:
        if ecu_id not in self._all_ecus_last_contact_timestamp:
            return False  # we have not yet connected to this ECU
        return (
            cur_timestamp
            > self._all_ecus_last_contact_timestamp[ecu_id]
            + self.UNREACHABLE_ECU_TIMEOUT
        )

    async def _properties_update(self):
        self.properties_last_update_timestamp = cur_timestamp = int(time.time())

        # check ECUs that lost contact
        self.lost_ecus_id = lost_ecus = set(
            (
                ecu_id
                for ecu_id in self._all_available_ecus_id
                if self._is_ecu_lost(ecu_id, cur_timestamp)
            )
        )

        # check ECUs that are updating
        self.updating_ecus = set(
            (
                status.ecu_id
                for status in chain(
                    self._all_ecus_status_v2.values(), self._all_ecus_status_v1.values()
                )
                if status.is_in_update and status.ecu_id not in lost_ecus
            )
        )
        self.any_in_update = len(self.updating_ecus) > 0

        # check if there is any failed child/self ECU
        self.failed_ecus = set(
            (
                status.ecu_id
                for status in chain(
                    self._all_ecus_status_v2.values(), self._all_ecus_status_v1.values()
                )
                if status.is_failed and status.ecu_id not in lost_ecus
            )
        )
        self.any_failed = len(self.failed_ecus) > 0

        # check if any ECUs require network
        self.any_requires_network = any(
            (
                status.if_requires_network
                for status in chain(
                    self._all_ecus_status_v2.values(), self._all_ecus_status_v1.values()
                )
                if status.ecu_id not in lost_ecus
            )
        )

        # check if all child ECUs and self ECU are in SUCCESS ota_status
        self.success_ecus = set(
            (
                status.ecu_id
                for status in chain(
                    self._all_ecus_status_v2.values(), self._all_ecus_status_v1.values()
                )
                if status.is_success and status.ecu_id not in lost_ecus
            )
        )
        self.all_success = len(self.success_ecus) == len(self._all_available_ecus_id)

        logger.debug(
            f"{self.updating_ecus=}, {self.failed_ecus=}, {self.any_requires_network=}, {self.success_ecus=}"
        )

    async def _loop_updating_properties(self):
        last_storage_update = self.storage_last_updated_timestamp
        while not self.properties_update_shutdown_event.is_set():
            # only update properties when storage is updated,
            # NOTE: if just receive update request, skip for DELAY
            #   seconds to prevent pre-mature property update.
            async with self._properties_update_lock:
                current_timestamp = int(time.time())
                if last_storage_update != self.storage_last_updated_timestamp and (
                    current_timestamp
                    > self.last_update_request_received_timestamp
                    + self.DELAY_PROPERTY_UPDATE
                ):
                    last_storage_update = self.storage_last_updated_timestamp
                    await self._properties_update()
            # if properties are not initialized, use active_interval for update
            if self.properties_last_update_timestamp == 0:
                await asyncio.sleep(self.ACTIVE_POLLING_INTERVAL)
            else:
                await asyncio.sleep(self.PROPERTY_REFRESH_INTERVAL)

    # API

    async def update_from_child_ECU(self, status_resp: wrapper.StatusResponse):
        """SubECUTracker calls this method to update storage with subECUs' status report."""
        async with self._writer_lock:
            self.storage_last_updated_timestamp = cur_timestamp = int(time.time())
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
            self.storage_last_updated_timestamp = cur_timestamp = int(time.time())
            self._all_available_ecus_id.add(ecu_status.ecu_id)

            ecu_id = ecu_status.ecu_id
            self._all_ecus_status_v2[ecu_id] = ecu_status
            self._all_ecus_last_contact_timestamp[ecu_id] = cur_timestamp

    async def on_receive_update_request(self, ecus_accept_update: Set[str]):
        """Update properties accordingly when ECUs accept update requests."""
        async with self._properties_update_lock:
            self.last_update_request_received_timestamp = int(time.time())
            self.updating_ecus = ecus_accept_update.copy()
            self.lost_ecus_id -= ecus_accept_update
            self.any_in_update = True
            self.any_requires_network = True
            self.failed_ecus -= ecus_accept_update
            self.any_failed = len(self.failed_ecus) > 0
            self.all_success = False
            self.success_ecus -= ecus_accept_update

    def get_polling_interval(self) -> int:
        """
        All polling task should poll actively when any_in_update(with small interval),
        otherwise poll idlely(with normal interval).
        """
        return (
            self.ACTIVE_POLLING_INTERVAL
            if self.any_in_update
            else self.IDLE_POLLING_INTERVAL
        )

    def export(self) -> wrapper.StatusResponse:
        res = wrapper.StatusResponse()
        res.available_ecu_ids.extend(self._all_available_ecus_id)

        ecu_using_v2 = set()
        for ecu_id, ecu_status_v2 in self._all_ecus_status_v2.items():
            res.add_ecu(ecu_status_v2)
            ecu_using_v2.add(ecu_id)

        for ecu_id, ecu_status_v1 in self._all_ecus_status_v1.items():
            if ecu_id in ecu_using_v2:
                continue  # if already populated by v2, skip
            res.add_ecu(ecu_status_v1)
        return res


class OTAClientServiceStub:
    OTAPROXY_SHUTDOWN_DELAY = cfg.OTAPROXY_SHUTDOWN_DELAY

    def __init__(self):
        self._executor = ThreadPoolExecutor(thread_name_prefix="otaclient_service_stub")
        self._run_in_executor = partial(
            asyncio.get_running_loop().run_in_executor, self._executor
        )

        self.ecu_info = ecu_info = ECUInfo.parse_ecu_info(cfg.ECU_INFO_FILE)
        self.listen_addr = ecu_info.ip_addr
        self.listen_port = server_cfg.SERVER_PORT
        self.my_ecu_id = ecu_info.ecu_id

        self._otaclient_control_flags = OTAClientControlFlags()
        self._otaclient_stub = OTAClientStub(
            ecu_info=ecu_info,
            executor=self._executor,
            control_flags=self._otaclient_control_flags,
            proxy=proxy_cfg.get_proxy_for_local_ota(),
        )
        self._otaproxy_launcher = OTAProxyLauncher(executor=self._executor)

        # ecu status tracking
        self._ecu_status_storage = ECUStatusStorage()
        self._ecu_status_polling_shutdown_event = asyncio.Event()
        # tracker for polling local ECU status
        asyncio.create_task(self._polling_local_ecu_status())
        # tracker for polling subECU status
        for ecu_contact in ecu_info.iter_direct_subecu_contact():
            asyncio.create_task(self._polling_direct_subecu_status(ecu_contact))

        # status monitoring tasks
        # as the dependency only caused by otaproxy, if local otaproxy is not enabled,
        # we skip the following logics.
        if self._otaproxy_launcher.enabled:
            self._status_checking_shutdown_event = asyncio.Event()
            asyncio.create_task(self._otaproxy_lifecycle_managing())
            asyncio.create_task(self._otaclient_control_flags_managing())
        else:
            # if otaproxy is not enabled, no dependent relationship will be formed,
            # always allow local otaclient to reboot
            self._otaclient_control_flags.set_can_reboot_flag()

    # internal

    async def _polling_direct_subecu_status(self, ecu_contact: ECUContact):
        """Task entry for loop polling one subECU's status."""
        while not self._ecu_status_polling_shutdown_event.is_set():
            try:
                _ecu_resp = await OtaClientCall.status_call(
                    ecu_contact.ecu_id,
                    ecu_contact.host,
                    ecu_contact.port,
                    timeout=server_cfg.SERVER_PORT,
                )
                await self._ecu_status_storage.update_from_child_ECU(_ecu_resp)
            except ECU_NO_RESPONSE as e:
                logger.warning(
                    f"ecu@{ecu_contact} doesn't respond to status request: {e!r}"
                )
            await asyncio.sleep(self._ecu_status_storage.get_polling_interval())

    async def _polling_local_ecu_status(self):
        """Task entry for loop polling local ECU status."""
        while not self._ecu_status_polling_shutdown_event.is_set():
            status_report = await self._otaclient_stub.get_status()
            await self._ecu_status_storage.update_from_local_ECU(status_report)
            await asyncio.sleep(self._ecu_status_storage.get_polling_interval())

    async def _otaproxy_lifecycle_managing(self):
        """Task entry for managing otaproxy's launching/shutdown."""
        otaproxy_last_launched_timestamp = 0
        while not self._status_checking_shutdown_event.is_set():
            cur_timestamp = int(time.time())
            any_requires_network = self._ecu_status_storage.any_requires_network
            if self._otaproxy_launcher.is_running:
                # NOTE: do not shutdown otaproxy too quick after it just starts!
                # If otaproxy just starts less than <OTAPROXY_SHUTDOWN_DELAY> seconds,
                # skip the shutdown
                if (
                    not any_requires_network
                    and cur_timestamp
                    > otaproxy_last_launched_timestamp + self.OTAPROXY_SHUTDOWN_DELAY
                ):
                    logger.info("stop otaproxy as not required")
                    await self._otaproxy_launcher.stop()
                    otaproxy_last_launched_timestamp = 0
            else:  # otaproxy is not running
                if any_requires_network:
                    logger.info("start otaproxy as required now")
                    await self._otaproxy_launcher.start(init_cache=False)
                    otaproxy_last_launched_timestamp = cur_timestamp
                # when otaproxy is not running and any_requires_network is False,
                # cleanup the cache dir when all ECUs are in SUCCESS ota_status
                elif self._ecu_status_storage.all_success:
                    self._otaproxy_launcher.cleanup_cache_dir()

            await asyncio.sleep(self._ecu_status_storage.get_polling_interval())

    async def _otaclient_control_flags_managing(self):
        """Task entry for set/clear otaclient control flags."""
        while not self._status_checking_shutdown_event.is_set():
            if not self._ecu_status_storage.any_requires_network:
                logger.debug("local otaclient can reboot as no ECU requires otaproxy")
                self._otaclient_control_flags.set_can_reboot_flag()
            else:
                logger.debug("local otaclient cannot reboot otaproxy is required")
                self._otaclient_control_flags.clear_can_reboot_flag()
            await asyncio.sleep(self._ecu_status_storage.get_polling_interval())

    # API stub

    async def update(self, request: wrapper.UpdateRequest) -> wrapper.UpdateResponse:
        logger.info(f"receive update request: {request}")
        update_acked_ecus = set()
        response = wrapper.UpdateResponse()

        # first: dispatch update request to all directly connected subECUs
        tasks: Dict[asyncio.Task, ECUContact] = {}
        for ecu_contact in self.ecu_info.iter_direct_subecu_contact():
            if not request.if_contains_ecu(ecu_contact.ecu_id):
                continue
            _task = asyncio.create_task(
                OtaClientCall.update_call(
                    ecu_contact.ecu_id,
                    ecu_contact.host,
                    ecu_contact.port,
                    request=request,
                    timeout=server_cfg.WAITING_SUBECU_ACK_REQ_TIMEOUT,
                )
            )
            tasks[_task] = ecu_contact

        # only collects task result if we have update request dispatched
        if tasks:
            done, _ = await asyncio.wait(tasks)
            for _task in done:
                try:
                    _ecu_resp: wrapper.UpdateResponse = _task.result()
                except ECU_NO_RESPONSE as e:
                    logger.warning(
                        f"{tasks[_task]} doesn't respond to update request: {e!r}"
                    )
                    continue
                update_acked_ecus.update(_ecu_resp.ecus_acked_update)
                response.merge_from(_ecu_resp)
            tasks.clear()

        # second: dispatch update request to local if required
        if update_req_ecu := request.find_update_meta(self.my_ecu_id):
            _resp_ecu = wrapper.UpdateResponseEcu(ecu_id=self.my_ecu_id)
            try:
                await self._otaclient_stub.dispatch_update(update_req_ecu)
                update_acked_ecus.add(self.my_ecu_id)
            except OTAClientBusy as e:
                logger.warning(f"self ECU is busy, ignore local update: {e!r}")
                _resp_ecu.result = wrapper.FailureType.RECOVERABLE
            response.add_ecu(_resp_ecu)

        # finally, trigger ecu_status_storage entering active mode if needed
        if update_acked_ecus:
            logger.info("at least one ECU accept update request")
            asyncio.create_task(
                self._ecu_status_storage.on_receive_update_request(update_acked_ecus)
            )
        return response

    async def rollback(
        self, request: wrapper.RollbackRequest
    ) -> wrapper.RollbackResponse:
        logger.info(f"receive rollback request: {request}")
        response = wrapper.RollbackResponse()

        # first: dispatch rollback request to all directly connected subECUs
        tasks: Dict[asyncio.Task, ECUContact] = {}
        for ecu_contact in self.ecu_info.iter_direct_subecu_contact():
            if not request.if_contains_ecu(ecu_contact.ecu_id):
                continue
            _task = asyncio.create_task(
                OtaClientCall.rollback_call(
                    ecu_contact.ecu_id,
                    ecu_contact.host,
                    ecu_contact.port,
                    request=request,
                    timeout=server_cfg.WAITING_SUBECU_ACK_REQ_TIMEOUT,
                )
            )
            tasks[_task] = ecu_contact

        # only collects task result if we have rollback request dispatched
        if tasks:
            done, _ = await asyncio.wait(tasks)
            for _task in done:
                try:
                    _ecu_resp: wrapper.RollbackResponse = _task.result()
                except ECU_NO_RESPONSE as e:
                    logger.warning(
                        f"{tasks[_task]} doesn't respond to rollback request: {e!r}"
                    )
                    continue
                response.merge_from(_ecu_resp)
            tasks.clear()

        # second: dispatch rollback request to local if required
        if rollback_req := request.find_rollback_req(self.my_ecu_id):
            _resp_ecu = wrapper.RollbackResponseEcu(ecu_id=self.my_ecu_id)
            try:
                await self._otaclient_stub.dispatch_rollback(rollback_req)
            except OTAClientBusy as e:
                logger.warning(f"self ECU is busy, ignore local rollback: {e!r}")
                _resp_ecu.result = wrapper.FailureType.RECOVERABLE
            response.add_ecu(_resp_ecu)
        return response

    async def status(
        self, _: Optional[wrapper.StatusRequest] = None
    ) -> wrapper.StatusResponse:
        return self._ecu_status_storage.export()
