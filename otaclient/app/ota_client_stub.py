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
    """Launcher of start/stop otaproxy in subprocess."""

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

    # API

    def cleanup_cache_dir(self):
        """
        NOTE: this method should only be called when all ECUs in the cluster
              are in SUCCESS ota_status(overall_ecu_status.all_success==True).
        """
        if (cache_dir := Path(self._proxy_server_cfg.BASE_DIR)).is_dir():
            logger.info("cleanup ota_cache on success")
            shutil.rmtree(cache_dir, ignore_errors=True)

    async def start(self, *, init_cache: bool) -> Optional[int]:
        """Start the otaproxy in a subprocess."""
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
        """Stop the otaproxy subprocess.

        NOTE: This method only shutdown the otaproxy process, it will not cleanup the
              cache dir. cache dir cleanup is handled by other mechanism.
              Check cleanup_cache_dir API for more details.
        """
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
    """Storage for holding ECU status reports from all ECUs in the cluster.

    This storage holds all ECUs' status gathered and reported by status polling tasks.
    The storage can be used to:
    1. generate response for the status API,
    2. tracking all child ECUs' status, generate overall ECU status report.

    The overall ECU status report will be generated with stored ECUs' status on
    every <PROPERTY_REFRESH_INTERVAL> seconds, if there is any update to the storage.

    Currently it will generate the following overall ECU status report:
    1. any_in_update:        at least one ECU in the cluster is doing OTA update or not,
    2. any_failed:           at least one ECU is in FAILURE ota_status or not,
    3. any_requires_network: at least one ECU requires network(for downloading) during update,
    4. all_success:          all ECUs are in SUCCESS ota_status or not,
    5. lost_ecus_id:         a list of ecu_ids of all disconnected ECUs,
    6. in_update_ecus_id:    a list of ecu_id of all updating ECUs,
    7. failed_ecus_id:       a list of ecu_id of all failed ECUs,
    8. success_ecus_id:      a list of ecu_id of all succeeded ECUs.

    NOTE:
        If ECU has been disconnected(doesn't respond to status probing) longer than <UNREACHABLE_TIMEOUT>,
        it will be treated as UNREACHABLE, listed in <lost_ecus_id>, and excluded when generating
        overall ECU status report (except lost_ecus_id).

    """

    DELAY_OVERALL_STATUS_REPORT_UPDATE = (
        cfg.KEEP_OVERALL_ECUS_STATUS_ON_ANY_UPDATE_REQ_ACKED
    )
    UNREACHABLE_ECU_TIMEOUT = cfg.ECU_UNREACHABLE_TIMEOUT
    PROPERTY_REFRESH_INTERVAL = cfg.OVERALL_ECUS_STATUS_UPDATE_INTERVAL
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

        # overall ECU status report
        self._properties_update_lock = asyncio.Lock()
        self.properties_last_update_timestamp = 0
        self.lost_ecus_id = set()
        self.in_update_ecus_id = set()
        self.any_in_update = False
        self.failed_ecus_id = set()
        self.any_failed = False
        self.any_requires_network = False
        self.success_ecus_id = set()
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

    async def _generate_overall_status_report(self):
        self.properties_last_update_timestamp = cur_timestamp = int(time.time())

        # check unreachable ECUs
        _old_lost_ecus_id = self.lost_ecus_id
        self.lost_ecus_id = lost_ecus = set(
            (
                ecu_id
                for ecu_id in self._all_available_ecus_id
                if self._is_ecu_lost(ecu_id, cur_timestamp)
            )
        )
        if _new_lost_ecus_id := lost_ecus.difference(_old_lost_ecus_id):
            logger.warning(
                f"new lost ecu(s)(disconnected longer than{self.UNREACHABLE_ECU_TIMEOUT}s)"
                f" detected: {_new_lost_ecus_id}, current {lost_ecus=}"
            )

        # check ECUs that are updating
        _old_in_update_ecus_id = self.in_update_ecus_id
        self.in_update_ecus_id = set(
            (
                status.ecu_id
                for status in chain(
                    self._all_ecus_status_v2.values(), self._all_ecus_status_v1.values()
                )
                if status.is_in_update and status.ecu_id not in lost_ecus
            )
        )
        self.any_in_update = len(self.in_update_ecus_id) > 0
        if _new_in_update_ecu := self.in_update_ecus_id.difference(
            _old_in_update_ecus_id
        ):
            logger.info(
                "new ECU(s) that acks update request and enters OTA update detected"
                f"{_new_in_update_ecu}, current {self.in_update_ecus_id=}"
            )

        # check if there is any failed child/self ECU
        _old_failed_ecus_id = self.failed_ecus_id
        self.failed_ecus_id = set(
            (
                status.ecu_id
                for status in chain(
                    self._all_ecus_status_v2.values(), self._all_ecus_status_v1.values()
                )
                if status.is_failed and status.ecu_id not in lost_ecus
            )
        )
        self.any_failed = len(self.failed_ecus_id) > 0
        if _new_failed_ecu := self.failed_ecus_id.difference(_old_failed_ecus_id):
            logger.warning(
                f"new failed ECU(s) detected: {_new_failed_ecu}, current {self.failed_ecus_id=}"
            )

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
        _old_all_success, _old_success_ecus_id = self.all_success, self.success_ecus_id
        self.success_ecus_id = set(
            (
                status.ecu_id
                for status in chain(
                    self._all_ecus_status_v2.values(), self._all_ecus_status_v1.values()
                )
                if status.is_success and status.ecu_id not in lost_ecus
            )
        )
        self.all_success = len(self.success_ecus_id) == len(self._all_available_ecus_id)
        if _new_success_ecu := self.success_ecus_id.difference(_old_success_ecus_id):
            logger.info(f"new succeeded ECU(s) detected: {_new_success_ecu}")
            if not _old_all_success and self.all_success:
                logger.info("all ECUs in the cluster are in SUCCESS ota_status")

        logger.debug(
            "overall ECU status reporrt updated:"
            f"{self.lost_ecus_id=}, {self.in_update_ecus_id=},{self.any_requires_network=}"
            f"{self.failed_ecus_id=}, {self.success_ecus_id=}, {self.all_success=}"
        )

    async def _loop_updating_properties(self):
        """ECU status storage's self generating overall ECU status report task.

        NOTE:
        1. only update properties when storage is updated,
        2. if just receive update request, skip generating new overall status report
            for <DELAY> seconds to prevent pre-mature status change.
            check on_receive_update_request method below for more details.
        """
        last_storage_update = self.storage_last_updated_timestamp
        while not self.properties_update_shutdown_event.is_set():
            async with self._properties_update_lock:
                current_timestamp = int(time.time())
                if last_storage_update != self.storage_last_updated_timestamp and (
                    current_timestamp
                    > self.last_update_request_received_timestamp
                    + self.DELAY_OVERALL_STATUS_REPORT_UPDATE
                ):
                    last_storage_update = self.storage_last_updated_timestamp
                    await self._generate_overall_status_report()
            # if properties are not initialized, use active_interval for update
            if self.properties_last_update_timestamp == 0:
                await asyncio.sleep(self.ACTIVE_POLLING_INTERVAL)
            else:
                await asyncio.sleep(self.PROPERTY_REFRESH_INTERVAL)

    # API

    async def update_from_child_ECU(self, status_resp: wrapper.StatusResponse):
        """Update the ECU status storage with child ECU's status report(StatusResponse)."""
        async with self._writer_lock:
            self.storage_last_updated_timestamp = cur_timestamp = int(time.time())
            self._all_available_ecus_id.update(status_resp.available_ecu_ids)

            # NOTE: explicitly support v1 format for backward-compatible with old otaclient
            for ecu_status_v2 in status_resp.ecu_v2:
                ecu_id = ecu_status_v2.ecu_id
                self._all_ecus_status_v2[ecu_id] = ecu_status_v2
                self._all_ecus_last_contact_timestamp[ecu_id] = cur_timestamp
            for ecu_status_v1 in status_resp.ecu:
                ecu_id = ecu_status_v1.ecu_id
                self._all_ecus_status_v1[ecu_id] = ecu_status_v1
                self._all_ecus_last_contact_timestamp[ecu_id] = cur_timestamp

    async def update_from_local_ECU(self, ecu_status: wrapper.StatusResponseEcuV2):
        """Update ECU status storage with local ECU's status report(StatusResponseEcuV2)."""
        async with self._writer_lock:
            self.storage_last_updated_timestamp = cur_timestamp = int(time.time())
            self._all_available_ecus_id.add(ecu_status.ecu_id)

            ecu_id = ecu_status.ecu_id
            self._all_ecus_status_v2[ecu_id] = ecu_status
            self._all_ecus_last_contact_timestamp[ecu_id] = cur_timestamp

    async def on_ecus_accept_update_request(self, ecus_accept_update: Set[str]):
        """Update overall ECU status report directly on ECU(s) accept OTA update request.

        for the ECUs that accepts OTA update request, we:
        1. add these ECUs' id into in_update_ecus_id set
        2. remove these ECUs' id from failed_ecus_id and success_ecus_id set
        3. remove these ECUs' id from lost_ecus_id set
        3. re-calculate overall ECUs status

        To prevent pre-mature overall status change(for example, the child ECU doesn't change
        their ota_status to UPDATING on-time due to status polling interval mismatch),
        the above set value will be kept for <DELAY_OVERALL_STATUS_REPORT_UPDATE> seconds.
        """
        async with self._properties_update_lock:
            self.last_update_request_received_timestamp = int(time.time())
            self.in_update_ecus_id.update(ecus_accept_update)
            self.lost_ecus_id -= ecus_accept_update
            self.any_in_update = True
            self.any_requires_network = True
            self.failed_ecus_id -= ecus_accept_update
            self.any_failed = len(self.failed_ecus_id) > 0
            self.all_success = False
            self.success_ecus_id -= ecus_accept_update

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
        """Export the contents of this storage to an instance of StatusResponse.

        NOTE: wrapper.StatusResponse's add_ecu method already takes care of
              v1 format backward-compatibility(input v2 format will result in
              v1 format and v2 format in the StatusResponse).
        """
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


class _ECUTracker:
    """Tracker that tracks and stores ECU status from all child ECUs and self ECU."""

    def __init__(
        self,
        ecu_status_storage: ECUStatusStorage,
        *,
        ecu_info: ECUInfo,
        otaclient_stub: OTAClientStub,
    ) -> None:
        self._otaclient_stub = otaclient_stub  # for local ECU status polling
        self._ecu_status_storage = ecu_status_storage
        self._ecu_status_polling_shutdown_event = asyncio.Event()

        # launch tracker for polling local ECU status
        asyncio.create_task(self._polling_local_ecu_status())
        # launch tracker for polling subECUs status
        for ecu_contact in ecu_info.iter_direct_subecu_contact():
            asyncio.create_task(self._polling_direct_subecu_status(ecu_contact))

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


class OTAClientServiceStub:
    """Handlers for otaclient service API.

    This class also handles otaproxy lifecyle and dependence managing.
    """

    OTAPROXY_SHUTDOWN_DELAY = cfg.OTAPROXY_MINIMUM_SHUTDOWN_INTERVAL

    def __init__(self, *, _proxy_cfg=proxy_cfg):
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
            proxy=_proxy_cfg.get_proxy_for_local_ota(),
        )

        # ecu status tracking
        self._ecu_status_storage = ECUStatusStorage()
        self._ecu_status_tracker = _ECUTracker(
            self._ecu_status_storage,
            ecu_info=ecu_info,
            otaclient_stub=self._otaclient_stub,
        )

        # otaproxy lifecycle and dependency managing
        if _proxy_cfg.enable_local_ota_proxy:
            self._otaproxy_launcher = OTAProxyLauncher(executor=self._executor)
            self._status_checking_shutdown_event = asyncio.Event()
            asyncio.create_task(self._otaproxy_lifecycle_managing())
            asyncio.create_task(self._otaclient_control_flags_managing())
        else:
            # if otaproxy is not enabled, no dependency relationship will be formed,
            # always allow local otaclient to reboot
            self._otaclient_control_flags.set_can_reboot_flag()

    # internal

    async def _otaproxy_lifecycle_managing(self):
        """Task entry for managing otaproxy's launching/shutdown.

        NOTE: cache_dir cleanup is handled here, when all ECUs are in SUCCESS ota_status,
              cache_dir will be removed.
        """
        otaproxy_last_launched_timestamp = 0
        while not self._status_checking_shutdown_event.is_set():
            cur_timestamp = int(time.time())
            any_requires_network = self._ecu_status_storage.any_requires_network
            if self._otaproxy_launcher.is_running:
                # NOTE: do not shutdown otaproxy too quick after it just starts!
                #       If otaproxy just starts less than <OTAPROXY_SHUTDOWN_DELAY> seconds,
                #       skip the shutdown this time.
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
        """Task entry for set/clear otaclient control flags.

        Prevent self ECU's reboot if there is dependency for otaproxy on this ECU.
        """
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
        if tasks:  # NOTE: input for asyncio.wait must not be empty!
            done, _ = await asyncio.wait(tasks)
            for _task in done:
                try:
                    _ecu_resp: wrapper.UpdateResponse = _task.result()
                except ECU_NO_RESPONSE as e:
                    logger.warning(
                        f"{tasks[_task]} doesn't respond to update request on-time"
                        f"(within {server_cfg.WAITING_SUBECU_ACK_REQ_TIMEOUT}s): {e!r}"
                    )
                    continue
                update_acked_ecus.update(_ecu_resp.ecus_acked_update)
                response.merge_from(_ecu_resp)
            tasks.clear()

        # second: dispatch update request to local if required by incoming request
        if update_req_ecu := request.find_update_meta(self.my_ecu_id):
            _resp_ecu = wrapper.UpdateResponseEcu(ecu_id=self.my_ecu_id)
            try:
                await self._otaclient_stub.dispatch_update(update_req_ecu)
                update_acked_ecus.add(self.my_ecu_id)
            except OTAClientBusy as e:
                logger.warning(f"self ECU is busy, ignore local update request: {e!r}")
                _resp_ecu.result = wrapper.FailureType.RECOVERABLE
            response.add_ecu(_resp_ecu)

        # finally, trigger ecu_status_storage entering active mode if needed
        if update_acked_ecus:
            logger.info("at least one ECU accept update request")
            asyncio.create_task(
                self._ecu_status_storage.on_ecus_accept_update_request(
                    update_acked_ecus
                )
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
        if tasks:  # NOTE: input for asyncio.wait must not be empty!
            done, _ = await asyncio.wait(tasks)
            for _task in done:
                try:
                    _ecu_resp: wrapper.RollbackResponse = _task.result()
                except ECU_NO_RESPONSE as e:
                    logger.warning(
                        f"{tasks[_task]} doesn't respond to rollback request on-time"
                        f"(within {server_cfg.WAITING_SUBECU_ACK_REQ_TIMEOUT}s): {e!r}"
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

    async def status(self, _=None) -> wrapper.StatusResponse:
        return self._ecu_status_storage.export()
