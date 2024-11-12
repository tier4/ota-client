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
"""OTA Service API v2 implementation."""


from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Dict

from otaclient.configs import ECUContact
from otaclient.configs.cfg import cfg, ecu_info, proxy_info
from otaclient.grpc._otaproxy_ctx import OTAProxyContext, OTAProxyLauncher
from otaclient.grpc.api_v2.ecu_status import ECUStatusStorage
from otaclient.grpc.api_v2.ecu_tracker import ECUTracker
from otaclient.ota_core import OTAClientControlFlags, OTAServicer
from otaclient_api.v2 import types as api_types
from otaclient_api.v2.api_caller import ECUNoResponse, OTAClientCall

logger = logging.getLogger(__name__)


class OTAClientAPIServicer:
    """Handlers for otaclient service API.

    This class also handles otaproxy lifecyle and dependence managing.
    """

    OTAPROXY_SHUTDOWN_DELAY = cfg.OTAPROXY_MINIMUM_SHUTDOWN_INTERVAL

    def __init__(self):
        self._executor = ThreadPoolExecutor(thread_name_prefix="otaclient_service_stub")
        self._run_in_executor = partial(
            asyncio.get_running_loop().run_in_executor, self._executor
        )

        self.sub_ecus = ecu_info.secondaries
        self.listen_addr = ecu_info.ip_addr
        self.listen_port = cfg.OTA_API_SERVER_PORT
        self.my_ecu_id = ecu_info.ecu_id

        self._otaclient_control_flags = OTAClientControlFlags()
        self._otaclient_wrapper = OTAServicer(
            executor=self._executor,
            control_flags=self._otaclient_control_flags,
            proxy=proxy_info.get_proxy_for_local_ota(),
        )

        # ecu status tracking
        self._ecu_status_storage = ECUStatusStorage()
        self._ecu_status_tracker = ECUTracker(
            self._ecu_status_storage,
            otaclient_wrapper=self._otaclient_wrapper,
        )

        self._polling_waiter = self._ecu_status_storage.get_polling_waiter()

        # otaproxy lifecycle and dependency managing
        # NOTE: _debug_status_checking_shutdown_event is for test only,
        #       allow us to stop background task without changing codes.
        #       In normal running this event will never be set.
        self._debug_status_checking_shutdown_event = asyncio.Event()
        if proxy_info.enable_local_ota_proxy:
            self._otaproxy_launcher = OTAProxyLauncher(
                executor=self._executor,
                subprocess_ctx=OTAProxyContext(),
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

    async def update(
        self, request: api_types.UpdateRequest
    ) -> api_types.UpdateResponse:
        logger.info(f"receive update request: {request}")
        update_acked_ecus = set()
        response = api_types.UpdateResponse()

        # first: dispatch update request to all directly connected subECUs
        tasks: Dict[asyncio.Task, ECUContact] = {}
        for ecu_contact in self.sub_ecus:
            if not request.if_contains_ecu(ecu_contact.ecu_id):
                continue
            _task = asyncio.create_task(
                OTAClientCall.update_call(
                    ecu_contact.ecu_id,
                    str(ecu_contact.ip_addr),
                    ecu_contact.port,
                    request=request,
                    timeout=cfg.WAITING_SUBECU_ACK_REQ_TIMEOUT,
                )
            )
            tasks[_task] = ecu_contact
        if tasks:  # NOTE: input for asyncio.wait must not be empty!
            done, _ = await asyncio.wait(tasks)
            for _task in done:
                try:
                    _ecu_resp: api_types.UpdateResponse = _task.result()
                    update_acked_ecus.update(_ecu_resp.ecus_acked_update)
                    response.merge_from(_ecu_resp)
                except ECUNoResponse as e:
                    _ecu_contact = tasks[_task]
                    logger.warning(
                        f"{_ecu_contact} doesn't respond to update request on-time"
                        f"(within {cfg.WAITING_SUBECU_ACK_REQ_TIMEOUT}s): {e!r}"
                    )
                    # NOTE(20230517): aligns with the previous behavior that create
                    #                 response with RECOVERABLE OTA error for unresponsive
                    #                 ECU.
                    response.add_ecu(
                        api_types.UpdateResponseEcu(
                            ecu_id=_ecu_contact.ecu_id,
                            result=api_types.FailureType.RECOVERABLE,
                        )
                    )
            tasks.clear()

        # second: dispatch update request to local if required by incoming request
        if update_req_ecu := request.find_ecu(self.my_ecu_id):
            _resp_ecu = await self._otaclient_wrapper.dispatch_update(update_req_ecu)
            # local otaclient accepts the update request
            if _resp_ecu.result == api_types.FailureType.NO_FAILURE:
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
        self, request: api_types.RollbackRequest
    ) -> api_types.RollbackResponse:
        logger.info(f"receive rollback request: {request}")
        response = api_types.RollbackResponse()

        # first: dispatch rollback request to all directly connected subECUs
        tasks: Dict[asyncio.Task, ECUContact] = {}
        for ecu_contact in self.sub_ecus:
            if not request.if_contains_ecu(ecu_contact.ecu_id):
                continue
            _task = asyncio.create_task(
                OTAClientCall.rollback_call(
                    ecu_contact.ecu_id,
                    str(ecu_contact.ip_addr),
                    ecu_contact.port,
                    request=request,
                    timeout=cfg.WAITING_SUBECU_ACK_REQ_TIMEOUT,
                )
            )
            tasks[_task] = ecu_contact
        if tasks:  # NOTE: input for asyncio.wait must not be empty!
            done, _ = await asyncio.wait(tasks)
            for _task in done:
                try:
                    _ecu_resp: api_types.RollbackResponse = _task.result()
                    response.merge_from(_ecu_resp)
                except ECUNoResponse as e:
                    _ecu_contact = tasks[_task]
                    logger.warning(
                        f"{_ecu_contact} doesn't respond to rollback request on-time"
                        f"(within {cfg.WAITING_SUBECU_ACK_REQ_TIMEOUT}s): {e!r}"
                    )
                    # NOTE(20230517): aligns with the previous behavior that create
                    #                 response with RECOVERABLE OTA error for unresponsive
                    #                 ECU.
                    response.add_ecu(
                        api_types.RollbackResponseEcu(
                            ecu_id=_ecu_contact.ecu_id,
                            result=api_types.FailureType.RECOVERABLE,
                        )
                    )
            tasks.clear()

        # second: dispatch rollback request to local if required
        if rollback_req := request.find_ecu(self.my_ecu_id):
            response.add_ecu(
                await self._otaclient_wrapper.dispatch_rollback(rollback_req)
            )

        return response

    async def status(self, _=None) -> api_types.StatusResponse:
        return await self._ecu_status_storage.export()
