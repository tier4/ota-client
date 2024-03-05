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
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from itertools import chain
from pathlib import Path
from typing import Any, Iterable, Optional, Set, Dict, Type, TypeVar
from typing_extensions import Self

from . import log_setting
from .configs import config as cfg, server_cfg
from .common import ensure_otaproxy_start
from .boot_control._common import CMDHelperFuncs
from .ecu_info import ECUContact, ECUInfo
from .ota_client import OTAClientControlFlags, OTAServicer
from .ota_client_call import ECUNoResponse, OtaClientCall
from .proto import wrapper
from .proxy_info import proxy_cfg

from otaclient.ota_proxy import (
    OTAProxyContextProto,
    subprocess_otaproxy_launcher,
    config as local_otaproxy_cfg,
)


logger = log_setting.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class _OTAProxyContext(OTAProxyContextProto):
    EXTERNAL_CACHE_KEY = "external_cache"

    def __init__(
        self,
        *,
        upper_proxy: Optional[str],
        external_cache_enabled: bool,
        external_cache_dev_fslable: str = cfg.EXTERNAL_CACHE_DEV_FSLABEL,
        external_cache_dev_mp: str = cfg.EXTERNAL_CACHE_DEV_MOUNTPOINT,
        external_cache_path: str = cfg.EXTERNAL_CACHE_SRC_PATH,
    ) -> None:
        self.upper_proxy = upper_proxy
        self.external_cache_enabled = external_cache_enabled

        self._external_cache_activated = False
        self._external_cache_dev_fslabel = external_cache_dev_fslable
        self._external_cache_dev = None  # type: ignore[assignment]
        self._external_cache_dev_mp = external_cache_dev_mp
        self._external_cache_data_dir = external_cache_path

        self.logger = logging.getLogger("otaclient.ota_proxy")

    @property
    def extra_kwargs(self) -> Dict[str, Any]:
        """Inject kwargs to otaproxy startup entry.

        Currently only inject <external_cache> if external cache storage is used.
        """
        _res = {}
        if self.external_cache_enabled and self._external_cache_activated:
            _res[self.EXTERNAL_CACHE_KEY] = self._external_cache_data_dir
        else:
            _res.pop(self.EXTERNAL_CACHE_KEY, None)
        return _res

    def _subprocess_init(self):
        """Initializing the subprocess before launching it."""
        # configure logging for otaproxy subprocess
        # NOTE: on otaproxy subprocess, we first set log level of the root logger
        #       to CRITICAL to filter out third_party libs' logging(requests, urllib3, etc.),
        #       and then set the otaclient.ota_proxy logger to DEFAULT_LOG_LEVEL
        log_setting.configure_logging(
            loglevel=logging.CRITICAL, ecu_id=log_setting.get_ecu_id()
        )
        otaproxy_logger = logging.getLogger("otaclient.ota_proxy")
        otaproxy_logger.setLevel(cfg.DEFAULT_LOG_LEVEL)
        self.logger = otaproxy_logger

        # wait for upper otaproxy if any
        if self.upper_proxy:
            otaproxy_logger.info(f"wait for {self.upper_proxy=} online...")
            ensure_otaproxy_start(self.upper_proxy)

    def _mount_external_cache_storage(self):
        # detect cache_dev on every startup
        _cache_dev = CMDHelperFuncs._findfs("LABEL", self._external_cache_dev_fslabel)
        if not _cache_dev:
            return

        self.logger.info(f"external cache dev detected at {_cache_dev}")
        self._external_cache_dev = _cache_dev

        # try to unmount the mount_point and cache_dev unconditionally
        _mp = Path(self._external_cache_dev_mp)
        CMDHelperFuncs.umount(_cache_dev, ignore_error=True)
        if _mp.is_dir():
            CMDHelperFuncs.umount(self._external_cache_dev_mp, ignore_error=True)
        else:
            _mp.mkdir(parents=True, exist_ok=True)

        # try to mount cache_dev ro
        try:
            CMDHelperFuncs.mount_ro(
                target=_cache_dev, mount_point=self._external_cache_dev_mp
            )
            self._external_cache_activated = True
        except Exception as e:
            logger.warning(
                f"failed to mount external cache dev({_cache_dev}) to {self._external_cache_dev_mp=}: {e!r}"
            )

    def _umount_external_cache_storage(self):
        if not self._external_cache_activated or not self._external_cache_dev:
            return
        try:
            CMDHelperFuncs.umount(self._external_cache_dev)
        except Exception as e:
            logger.warning(
                f"failed to unmount external cache_dev {self._external_cache_dev}: {e!r}"
            )
        finally:
            self.started = self._external_cache_activated = False

    def __enter__(self) -> Self:
        try:
            self._subprocess_init()
            self._mount_external_cache_storage()
            return self
        except Exception as e:
            # if subprocess init failed, directly let the process exit
            self.logger.error(f"otaproxy subprocess init failed, exit: {e!r}")
            sys.exit(1)

    def __exit__(
        self,
        __exc_type: Optional[Type[BaseException]],
        __exc_value: Optional[BaseException],
        __traceback,
    ) -> Optional[bool]:
        if __exc_type:
            _exc = __exc_value if __exc_value else __exc_type()
            self.logger.warning(f"exception during otaproxy shutdown: {_exc!r}")
        # otaproxy post-shutdown cleanup:
        #   1. umount external cache storage
        self._umount_external_cache_storage()


class OTAProxyLauncher:
    """Launcher of start/stop otaproxy in subprocess."""

    def __init__(
        self,
        *,
        executor: ThreadPoolExecutor,
        subprocess_ctx: OTAProxyContextProto,
        _proxy_info=proxy_cfg,
        _proxy_server_cfg=local_otaproxy_cfg,
    ) -> None:
        self._proxy_info = _proxy_info
        self._proxy_server_cfg = _proxy_server_cfg
        self.enabled = _proxy_info.enable_local_ota_proxy
        self.upper_otaproxy = _proxy_info.upper_ota_proxy
        self.subprocess_ctx = subprocess_ctx

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
            _subprocess_entry = subprocess_otaproxy_launcher(
                subprocess_ctx=self.subprocess_ctx
            )
            otaproxy_subprocess = await self._run_in_executor(
                partial(
                    _subprocess_entry,
                    host=self._proxy_info.local_ota_proxy_listen_addr,
                    port=self._proxy_info.local_ota_proxy_listen_port,
                    init_cache=init_cache,
                    cache_dir=self._proxy_server_cfg.BASE_DIR,
                    cache_db_f=self._proxy_server_cfg.DB_FILE,
                    upper_proxy=self.upper_otaproxy,
                    enable_cache=self._proxy_info.enable_local_ota_proxy_cache,
                    enable_https=self._proxy_info.gateway,
                )
            )
            self._otaproxy_subprocess = otaproxy_subprocess
            logger.info(
                f"otaproxy({otaproxy_subprocess.pid=}) started at "
                f"{self._proxy_info.local_ota_proxy_listen_addr}:{self._proxy_info.local_ota_proxy_listen_port}"
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
                logger.info("shuting down otaproxy server process...")
                self._otaproxy_subprocess.terminate()
                self._otaproxy_subprocess.join()
            self._otaproxy_subprocess = None

        async with self._lock:
            await self._run_in_executor(_shutdown)
            logger.info("otaproxy closed")


T = TypeVar("T")


class _OrderedSet(Dict[T, None]):
    def __init__(self, _input: Optional[Iterable[T]]):
        if _input:
            for elem in _input:
                self[elem] = None
        super().__init__()

    def add(self, value: T):
        self[value] = None

    def remove(self, value: T):
        super().pop(value)

    def discard(self, value: T):
        super().pop(value, None)


class ECUStatusStorage:
    """Storage for holding ECU status reports from all ECUs in the cluster.

    This storage holds all ECUs' status gathered and reported by status polling tasks.
    The storage can be used to:
    1. generate response for the status API,
    2. tracking all child ECUs' status, generate overall ECU status report.

    The overall ECU status report will be generated with stored ECUs' status on
    every <PROPERTY_REFRESH_INTERVAL> seconds, if there is any update to the storage.

    Currently it will generate the following overall ECU status report:
    1. any_requires_network:        at least one ECU requires network(for downloading) during update,
    2. all_success:                 all ECUs are in SUCCESS ota_status or not,
    3. lost_ecus_id:                a list of ecu_ids of all disconnected ECUs,
    4. in_update_ecus_id:           a list of ecu_ids of all updating ECUs,
    5. failed_ecus_id:              a list of ecu_ids of all failed ECUs,
    6. success_ecus_id:             a list of ecu_ids of all succeeded ECUs,
    7. in_update_childecus_id:      a list of ecu_ids of all updating child ECUs.

    NOTE:
        If ECU has been disconnected(doesn't respond to status probing) longer than <UNREACHABLE_TIMEOUT>,
        it will be treated as UNREACHABLE and listed in <lost_ecus_id>, further being excluded when generating
        any_requires_network, all_success, in_update_ecus_id, failed_ecus_id, success_ecus_id and in_update_childecus_id.
    """

    DELAY_OVERALL_STATUS_REPORT_UPDATE = (
        cfg.KEEP_OVERALL_ECUS_STATUS_ON_ANY_UPDATE_REQ_ACKED
    )
    UNREACHABLE_ECU_TIMEOUT = cfg.ECU_UNREACHABLE_TIMEOUT
    PROPERTY_REFRESH_INTERVAL = cfg.OVERALL_ECUS_STATUS_UPDATE_INTERVAL
    IDLE_POLLING_INTERVAL = cfg.IDLE_INTERVAL
    ACTIVE_POLLING_INTERVAL = cfg.ACTIVE_INTERVAL

    # NOTE(20230522):
    #   ECU will be treated as disconnected if we cannot get in touch with it
    #   longer than <DISCONNECTED_ECU_TIMEOUT_FACTOR> * <current_polling_interval>.
    #   disconnected ECU will be excluded from status API response.
    DISCONNECTED_ECU_TIMEOUT_FACTOR = 3

    def __init__(self, ecu_info: ECUInfo) -> None:
        self.my_ecu_id = ecu_info.ecu_id
        self._writer_lock = asyncio.Lock()
        # ECU status storage
        self.storage_last_updated_timestamp = 0

        # ECUs that are/will be active during an OTA session,
        #   at init it will be the ECUs listed in available_ecu_ids defined
        #   in ecu_info.yaml.
        # When receives update request, the list will be set to include ECUs
        #   listed in the update request, and be extended by merging
        #   available_ecu_ids in sub ECUs' status report.
        # Internally referenced when generating overall ECU status report.
        # TODO: in the future if otaclient can preserve OTA session info,
        #       ECUStatusStorage should restore the tracked_active_ecus info
        #       in the saved session info.
        self._tracked_active_ecus: _OrderedSet[str] = _OrderedSet(
            ecu_info.get_available_ecu_ids()
        )

        # The attribute that will be exported in status API response,
        # NOTE(20230801): available_ecu_ids only serves information purpose,
        #                 it should only be updated with ecu_info.yaml or merging
        #                 available_ecu_ids field in sub ECUs' status report.
        # NOTE(20230801): for web.auto user, available_ecu_ids in status API response
        #                 will be used to generate update request list, so be-careful!
        self._available_ecu_ids: _OrderedSet[str] = _OrderedSet(
            ecu_info.get_available_ecu_ids()
        )

        self._all_ecus_status_v2: Dict[str, wrapper.StatusResponseEcuV2] = {}
        self._all_ecus_status_v1: Dict[str, wrapper.StatusResponseEcu] = {}
        self._all_ecus_last_contact_timestamp: Dict[str, int] = {}

        # overall ECU status report
        self._properties_update_lock = asyncio.Lock()
        self.properties_last_update_timestamp = 0
        self.active_ota_update_present = asyncio.Event()

        self.lost_ecus_id = set()
        self.failed_ecus_id = set()

        self.in_update_ecus_id = set()
        self.in_update_child_ecus_id = set()
        self.any_requires_network = False

        self.success_ecus_id = set()
        self.all_success = False

        # property update task
        # NOTE: _debug_properties_update_shutdown_event is for test only,
        #       allow us to stop background task without changing codes.
        #       In normal running this event will never be set.
        self._debug_properties_update_shutdown_event = asyncio.Event()
        asyncio.create_task(self._loop_updating_properties())

        # on receive update request
        self.last_update_request_received_timestamp = 0

    def _is_ecu_lost(self, ecu_id: str, cur_timestamp: int) -> bool:
        if ecu_id not in self._all_ecus_last_contact_timestamp:
            return True  # we have not yet connected to this ECU
        return (
            cur_timestamp
            > self._all_ecus_last_contact_timestamp[ecu_id]
            + self.UNREACHABLE_ECU_TIMEOUT
        )

    async def _generate_overall_status_report(self):
        """Generate overall status report against tracked active OTA ECUs.

        NOTE: as special case, lost_ecus set is calculated against all reachable ECUs.
        """
        self.properties_last_update_timestamp = cur_timestamp = int(time.time())

        # check unreachable ECUs
        # NOTE(20230801): this property is calculated against all reachable ECUs,
        #                 including further child ECUs.
        _old_lost_ecus_id = self.lost_ecus_id
        self.lost_ecus_id = lost_ecus = set(
            (
                ecu_id
                for ecu_id in self._all_ecus_last_contact_timestamp
                if self._is_ecu_lost(ecu_id, cur_timestamp)
            )
        )
        if _new_lost_ecus_id := lost_ecus.difference(_old_lost_ecus_id):
            logger.warning(f"new lost ecu(s) detected: {_new_lost_ecus_id}")
        if lost_ecus:
            logger.warning(
                f"lost ecu(s)(disconnected longer than{self.UNREACHABLE_ECU_TIMEOUT}s): {lost_ecus=}"
            )

        # check ECUs in tracked active ECUs set that are updating
        _old_in_update_ecus_id = self.in_update_ecus_id
        self.in_update_ecus_id = in_update_ecus_id = set(
            (
                status.ecu_id
                for status in chain(
                    self._all_ecus_status_v2.values(), self._all_ecus_status_v1.values()
                )
                if status.ecu_id in self._tracked_active_ecus
                and status.is_in_update
                and status.ecu_id not in lost_ecus
            )
        )
        self.in_update_child_ecus_id = in_update_ecus_id - {self.my_ecu_id}
        if _new_in_update_ecu := in_update_ecus_id.difference(_old_in_update_ecus_id):
            logger.info(
                "new ECU(s) that acks update request and enters OTA update detected"
                f"{_new_in_update_ecu}, current updating ECUs: {in_update_ecus_id}"
            )
        if in_update_ecus_id:
            self.active_ota_update_present.set()
        else:
            self.active_ota_update_present.clear()

        # check if there is any failed child/self ECU in tracked active ECUs set
        _old_failed_ecus_id = self.failed_ecus_id
        self.failed_ecus_id = set(
            (
                status.ecu_id
                for status in chain(
                    self._all_ecus_status_v2.values(), self._all_ecus_status_v1.values()
                )
                if status.ecu_id in self._tracked_active_ecus
                and status.is_failed
                and status.ecu_id not in lost_ecus
            )
        )
        if _new_failed_ecu := self.failed_ecus_id.difference(_old_failed_ecus_id):
            logger.warning(
                f"new failed ECU(s) detected: {_new_failed_ecu}, current {self.failed_ecus_id=}"
            )

        # check if any ECUs in the tracked tracked active ECUs set require network
        self.any_requires_network = any(
            (
                status.requires_network
                for status in chain(
                    self._all_ecus_status_v2.values(), self._all_ecus_status_v1.values()
                )
                if status.ecu_id in self._tracked_active_ecus
                and status.ecu_id not in lost_ecus
            )
        )

        # check if all tracked active_ota_ecus are in SUCCESS ota_status
        _old_all_success, _old_success_ecus_id = self.all_success, self.success_ecus_id
        self.success_ecus_id = set(
            (
                status.ecu_id
                for status in chain(
                    self._all_ecus_status_v2.values(), self._all_ecus_status_v1.values()
                )
                if status.ecu_id in self._tracked_active_ecus
                and status.is_success
                and status.ecu_id not in lost_ecus
            )
        )
        # NOTE: all_success doesn't count the lost ECUs
        self.all_success = len(self.success_ecus_id) == len(self._tracked_active_ecus)
        if _new_success_ecu := self.success_ecus_id.difference(_old_success_ecus_id):
            logger.info(f"new succeeded ECU(s) detected: {_new_success_ecu}")
            if not _old_all_success and self.all_success:
                logger.info("all ECUs in the cluster are in SUCCESS ota_status")

        logger.debug(
            "overall ECU status reporrt updated:"
            f"{self.lost_ecus_id=}, {self.in_update_ecus_id=},{self.any_requires_network=},"
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
        while not self._debug_properties_update_shutdown_event.is_set():
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

    async def update_from_child_ecu(self, status_resp: wrapper.StatusResponse):
        """Update the ECU status storage with child ECU's status report(StatusResponse)."""
        async with self._writer_lock:
            self.storage_last_updated_timestamp = cur_timestamp = int(time.time())
            _subecu_available_ecu_ids = _OrderedSet(status_resp.available_ecu_ids)
            # discover further child ECUs from directly connected sub ECUs.
            self._tracked_active_ecus.update(_subecu_available_ecu_ids)
            # merge available_ecu_ids from child ECUs resp to include further child ECUs
            self._available_ecu_ids.update(_subecu_available_ecu_ids)

            # NOTE: use v2 if v2 is available, but explicitly support v1 format
            #       for backward-compatible with old otaclient
            # NOTE: edge condition of ECU doing OTA downgrade to old image with old otaclient,
            #       this ECU will report status in v1 when downgrade is finished! So if we use
            #       v1 status report, we should remove the entry in v2, vice versa.
            _processed_ecus_id = set()
            for ecu_status_v2 in status_resp.iter_ecu_v2():
                ecu_id = ecu_status_v2.ecu_id
                self._all_ecus_status_v2[ecu_id] = ecu_status_v2
                self._all_ecus_last_contact_timestamp[ecu_id] = cur_timestamp
                self._all_ecus_status_v1.pop(ecu_id, None)
                _processed_ecus_id.add(ecu_id)

            for ecu_status_v1 in status_resp.iter_ecu():
                ecu_id = ecu_status_v1.ecu_id
                if ecu_id in _processed_ecus_id:
                    continue  # use v2 in prior
                self._all_ecus_status_v1[ecu_id] = ecu_status_v1
                self._all_ecus_last_contact_timestamp[ecu_id] = cur_timestamp
                self._all_ecus_status_v2.pop(ecu_id, None)

    async def update_from_local_ecu(self, ecu_status: wrapper.StatusResponseEcuV2):
        """Update ECU status storage with local ECU's status report(StatusResponseEcuV2)."""
        async with self._writer_lock:
            self.storage_last_updated_timestamp = cur_timestamp = int(time.time())

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
        4. reset tracked_active_ecus list to <ecus_accept_update>

        To prevent pre-mature overall status change(for example, the child ECU doesn't change
        their ota_status to UPDATING on-time due to status polling interval mismatch),
        the above set value will be kept for <DELAY_OVERALL_STATUS_REPORT_UPDATE> seconds.
        """
        async with self._properties_update_lock:
            self._tracked_active_ecus = _OrderedSet(ecus_accept_update)

            self.last_update_request_received_timestamp = int(time.time())
            self.lost_ecus_id -= ecus_accept_update
            self.failed_ecus_id -= ecus_accept_update

            self.in_update_ecus_id.update(ecus_accept_update)
            self.in_update_child_ecus_id = self.in_update_ecus_id - {self.my_ecu_id}

            self.any_requires_network = True
            self.all_success = False
            self.success_ecus_id -= ecus_accept_update

            self.active_ota_update_present.set()

    def get_polling_interval(self) -> int:
        """Return <ACTIVE_POLLING_INTERVAL> if there is active OTA update,
        otherwise <IDLE_POLLING_INTERVAL>.

        NOTE: use get_polling_waiter if want to wait, only call this method
            if one only wants to get the polling interval value.
        """
        return (
            self.ACTIVE_POLLING_INTERVAL
            if self.active_ota_update_present.is_set()
            else self.IDLE_POLLING_INTERVAL
        )

    def get_polling_waiter(self):
        """Get a executable async waiter which waiting time is decided by
        whether there is active OTA updating or not.

        This waiter will wait for <ACTIVE_POLLING_INTERVAL> and then return
            if there is active OTA in the cluster.
        Or if the cluster doesn't have active OTA, wait <IDLE_POLLING_INTERVAL>
            or self.active_ota_update_present is set, return when one of the
            condition is met.
        """

        async def _waiter():
            if self.active_ota_update_present.is_set():
                await asyncio.sleep(self.ACTIVE_POLLING_INTERVAL)
                return

            try:
                await asyncio.wait_for(
                    self.active_ota_update_present.wait(),
                    timeout=self.IDLE_POLLING_INTERVAL,
                )
            except asyncio.TimeoutError:
                return

        return _waiter

    async def export(self) -> wrapper.StatusResponse:
        """Export the contents of this storage to an instance of StatusResponse.

        NOTE: wrapper.StatusResponse's add_ecu method already takes care of
              v1 format backward-compatibility(input v2 format will result in
              v1 format and v2 format in the StatusResponse).
        NOTE: to align with preivous behavior that disconnected ECU should have no
              entry in status API response, simulate this behavior by skipping
              disconnected ECU's status report entry.
        """
        res = wrapper.StatusResponse()

        async with self._writer_lock:
            res.available_ecu_ids.extend(self._available_ecu_ids)

            # NOTE(20230802): export all reachable ECUs' status, no matter they are in
            #                 active OTA or not.
            # NOTE: ECU status for specific ECU will not appear at both v1 and v2 list,
            #       this is guaranteed by the update_from_child_ecu API method.
            for ecu_id in self._all_ecus_last_contact_timestamp:
                # NOTE: skip this ECU if it doesn't respond recently enough,
                #       to signal the agent that this ECU doesn't respond.
                _timout = (
                    self._all_ecus_last_contact_timestamp.get(ecu_id, 0)
                    + self.DISCONNECTED_ECU_TIMEOUT_FACTOR * self.get_polling_interval()
                )
                if self.storage_last_updated_timestamp > _timout:
                    continue

                _ecu_status_rep = self._all_ecus_status_v2.get(
                    ecu_id, self._all_ecus_status_v1.get(ecu_id, None)
                )
                if _ecu_status_rep:
                    res.add_ecu(_ecu_status_rep)
            return res


class _ECUTracker:
    """Tracker that queries and stores ECU status from all defined ECUs."""

    def __init__(
        self,
        ecu_status_storage: ECUStatusStorage,
        *,
        ecu_info: ECUInfo,
        otaclient_wrapper: OTAServicer,
    ) -> None:
        self._otaclient_wrapper = otaclient_wrapper  # for local ECU status polling
        self._ecu_status_storage = ecu_status_storage
        self._polling_waiter = self._ecu_status_storage.get_polling_waiter()

        # launch ECU trackers for all defined ECUs
        # NOTE: _debug_ecu_status_polling_shutdown_event is for test only,
        #       allow us to stop background task without changing codes.
        #       In normal running this event will never be set.
        self._debug_ecu_status_polling_shutdown_event = asyncio.Event()
        asyncio.create_task(self._polling_local_ecu_status())
        for ecu_contact in ecu_info.iter_direct_subecu_contact():
            asyncio.create_task(self._polling_direct_subecu_status(ecu_contact))

    async def _polling_direct_subecu_status(self, ecu_contact: ECUContact):
        """Task entry for loop polling one subECU's status."""
        while not self._debug_ecu_status_polling_shutdown_event.is_set():
            try:
                _ecu_resp = await OtaClientCall.status_call(
                    ecu_contact.ecu_id,
                    ecu_contact.host,
                    ecu_contact.port,
                    timeout=server_cfg.QUERYING_SUBECU_STATUS_TIMEOUT,
                    request=wrapper.StatusRequest(),
                )
                await self._ecu_status_storage.update_from_child_ecu(_ecu_resp)
            except ECUNoResponse as e:
                logger.debug(
                    f"ecu@{ecu_contact} doesn't respond to status request: {e!r}"
                )
            await self._polling_waiter()

    async def _polling_local_ecu_status(self):
        """Task entry for loop polling local ECU status."""
        while not self._debug_ecu_status_polling_shutdown_event.is_set():
            status_report = await self._otaclient_wrapper.get_status()
            await self._ecu_status_storage.update_from_local_ecu(status_report)
            await self._polling_waiter()


class OTAClientServiceStub:
    """Handlers for otaclient service API.

    This class also handles otaproxy lifecyle and dependence managing.
    """

    OTAPROXY_SHUTDOWN_DELAY = cfg.OTAPROXY_MINIMUM_SHUTDOWN_INTERVAL

    def __init__(self, *, ecu_info: ECUInfo, _proxy_cfg=proxy_cfg):
        self._executor = ThreadPoolExecutor(thread_name_prefix="otaclient_service_stub")
        self._run_in_executor = partial(
            asyncio.get_running_loop().run_in_executor, self._executor
        )

        self.ecu_info = ecu_info
        self.listen_addr = ecu_info.ip_addr
        self.listen_port = server_cfg.SERVER_PORT
        self.my_ecu_id = ecu_info.ecu_id

        self._otaclient_control_flags = OTAClientControlFlags()
        self._otaclient_wrapper = OTAServicer(
            ecu_info=ecu_info,
            executor=self._executor,
            control_flags=self._otaclient_control_flags,
            proxy=_proxy_cfg.get_proxy_for_local_ota(),
        )

        # ecu status tracking
        self._ecu_status_storage = ECUStatusStorage(ecu_info)
        self._ecu_status_tracker = _ECUTracker(
            self._ecu_status_storage,
            ecu_info=ecu_info,
            otaclient_wrapper=self._otaclient_wrapper,
        )

        self._polling_waiter = self._ecu_status_storage.get_polling_waiter()

        # otaproxy lifecycle and dependency managing
        # NOTE: _debug_status_checking_shutdown_event is for test only,
        #       allow us to stop background task without changing codes.
        #       In normal running this event will never be set.
        self._debug_status_checking_shutdown_event = asyncio.Event()
        if _proxy_cfg.enable_local_ota_proxy:
            self._otaproxy_launcher = OTAProxyLauncher(
                executor=self._executor,
                subprocess_ctx=_OTAProxyContext(
                    upper_proxy=_proxy_cfg.upper_ota_proxy,
                    # NOTE: default enable detecting external cache storage
                    external_cache_enabled=True,
                ),
            )
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
        while not self._debug_status_checking_shutdown_event.is_set():
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
                    await self._otaproxy_launcher.stop()
                    otaproxy_last_launched_timestamp = 0
            else:  # otaproxy is not running
                if any_requires_network:
                    await self._otaproxy_launcher.start(init_cache=False)
                    otaproxy_last_launched_timestamp = cur_timestamp
                # when otaproxy is not running and any_requires_network is False,
                # cleanup the cache dir when all ECUs are in SUCCESS ota_status
                elif self._ecu_status_storage.all_success:
                    self._otaproxy_launcher.cleanup_cache_dir()
            await self._polling_waiter()

    async def _otaclient_control_flags_managing(self):
        """Task entry for set/clear otaclient control flags.

        Prevent self ECU from rebooting when their is at least one ECU
        under UPDATING ota_status.
        """
        while not self._debug_status_checking_shutdown_event.is_set():
            _can_reboot = self._otaclient_control_flags.is_can_reboot_flag_set()
            if not self._ecu_status_storage.in_update_child_ecus_id:
                if not _can_reboot:
                    logger.info(
                        "local otaclient can reboot as no child ECU is in UPDATING ota_status"
                    )
                self._otaclient_control_flags.set_can_reboot_flag()
            else:
                if _can_reboot:
                    logger.info(
                        f"local otaclient cannot reboot as child ECUs {self._ecu_status_storage.in_update_child_ecus_id}"
                        " are in UPDATING ota_status"
                    )
                self._otaclient_control_flags.clear_can_reboot_flag()
            await self._polling_waiter()

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
                    update_acked_ecus.update(_ecu_resp.ecus_acked_update)
                    response.merge_from(_ecu_resp)
                except ECUNoResponse as e:
                    _ecu_contact = tasks[_task]
                    logger.warning(
                        f"{_ecu_contact} doesn't respond to update request on-time"
                        f"(within {server_cfg.WAITING_SUBECU_ACK_REQ_TIMEOUT}s): {e!r}"
                    )
                    # NOTE(20230517): aligns with the previous behavior that create
                    #                 response with RECOVERABLE OTA error for unresponsive
                    #                 ECU.
                    response.add_ecu(
                        wrapper.UpdateResponseEcu(
                            ecu_id=_ecu_contact.ecu_id,
                            result=wrapper.FailureType.RECOVERABLE,
                        )
                    )
            tasks.clear()

        # second: dispatch update request to local if required by incoming request
        if update_req_ecu := request.find_ecu(self.my_ecu_id):
            _resp_ecu = await self._otaclient_wrapper.dispatch_update(update_req_ecu)
            # local otaclient accepts the update request
            if _resp_ecu.result == wrapper.FailureType.NO_FAILURE:
                update_acked_ecus.add(self.my_ecu_id)
            response.add_ecu(_resp_ecu)

        # finally, trigger ecu_status_storage entering active mode if needed
        if update_acked_ecus:
            logger.info(f"ECUs accept OTA request: {update_acked_ecus}")
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
                    response.merge_from(_ecu_resp)
                except ECUNoResponse as e:
                    _ecu_contact = tasks[_task]
                    logger.warning(
                        f"{_ecu_contact} doesn't respond to rollback request on-time"
                        f"(within {server_cfg.WAITING_SUBECU_ACK_REQ_TIMEOUT}s): {e!r}"
                    )
                    # NOTE(20230517): aligns with the previous behavior that create
                    #                 response with RECOVERABLE OTA error for unresponsive
                    #                 ECU.
                    response.add_ecu(
                        wrapper.RollbackResponseEcu(
                            ecu_id=_ecu_contact.ecu_id,
                            result=wrapper.FailureType.RECOVERABLE,
                        )
                    )
            tasks.clear()

        # second: dispatch rollback request to local if required
        if rollback_req := request.find_ecu(self.my_ecu_id):
            response.add_ecu(
                await self._otaclient_wrapper.dispatch_rollback(rollback_req)
            )

        return response

    async def status(self, _=None) -> wrapper.StatusResponse:
        return await self._ecu_status_storage.export()
