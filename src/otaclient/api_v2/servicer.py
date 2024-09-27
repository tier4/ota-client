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


from __future__ import annotations

import asyncio
import atexit
import logging
import multiprocessing as mp
import multiprocessing.synchronize as mp_sync
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import otaclient_api.v2.otaclient_v2_pb2 as pb2
import otaclient_api.v2.otaclient_v2_pb2_grpc as pb2_grpc
import otaclient_api.v2.types as api_types
from otaclient._types import (
    OTAClientStatus,
    OTAOperationResp,
    RollbackRequestV2,
    UpdateRequestV2,
)
from otaclient.api_v2.ecu_status import ECUStatusStorage, ECUTracker
from otaclient.api_v2.otaproxy_ctx import OTAProxyContext, OTAProxyLauncher
from otaclient.app.configs import config as cfg
from otaclient.app.configs import ecu_info, proxy_info, server_cfg
from otaclient.configs.ecu_info import ECUContact
from otaclient_api.v2.api_caller import ECUNoResponse, OTAClientCall

logger = logging.getLogger(__name__)

_otaclient_shutdown = False


def _global_shutdown():
    global _otaclient_shutdown
    _otaclient_shutdown = True


atexit.register(_global_shutdown)


OTA_SESSION_LOCK_CHECK_INTERVAL = 6
OTA_SESSION_LOCK_MINIMUM_OTA_TIME = 60
_MAX_WAIT_RESP_TIME = 10


class _OTAClientAPIServicer:
    """Handlers for otaclient service API.

    This class also handles otaproxy lifecyle and dependence managing.
    """

    OTAPROXY_SHUTDOWN_DELAY = cfg.OTAPROXY_MINIMUM_SHUTDOWN_INTERVAL

    def __init__(
        self,
        *,
        status_report_queue: mp.Queue[OTAClientStatus],
        operation_push_queue: mp.Queue,
        operation_ack_queue: mp.Queue,
        reboot_flag: mp_sync.Event,
    ):
        self._executor = ThreadPoolExecutor(
            max_workers=3, thread_name_prefix="api_servicer_threadpool"
        )
        self._run_in_executor = partial(
            asyncio.get_running_loop().run_in_executor, self._executor
        )
        self._operation_push_queue = operation_push_queue
        self._operation_ack_queue = operation_ack_queue

        self.sub_ecus = ecu_info.secondaries
        self.listen_addr = ecu_info.ip_addr
        self.listen_port = server_cfg.SERVER_PORT
        self.my_ecu_id = ecu_info.ecu_id

        self._reboot_flag = reboot_flag

        # start ecu status tracking
        self._ecu_status_storage = ECUStatusStorage()
        self._ecu_status_tracker = ECUTracker(
            ecu_status_storage=self._ecu_status_storage,
            status_report_queue=status_report_queue,
        )

        self._polling_waiter = self._ecu_status_storage.get_polling_waiter()

        # otaproxy lifecycle and dependency managing
        if proxy_info.enable_local_ota_proxy:
            self._otaproxy_launcher = OTAProxyLauncher(
                executor=self._executor,
                subprocess_ctx=OTAProxyContext(),
            )
            asyncio.create_task(self._otaproxy_lifecycle_managing())
            asyncio.create_task(self._otaclient_reboot_flag_managing())
        else:
            # if otaproxy is not enabled, no dependency relationship will be formed,
            # always allow local otaclient to reboot
            reboot_flag.set()

    # internal

    async def _otaproxy_lifecycle_managing(self):
        """Task entry for managing otaproxy's launching/shutdown.

        NOTE: cache_dir cleanup is handled here, when all ECUs are in SUCCESS ota_status,
              cache_dir will be removed.
        """
        otaproxy_last_launched_timestamp = 0
        while not _otaclient_shutdown:
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

    async def _otaclient_reboot_flag_managing(self):
        """Task entry for set/clear otaclient permit reboot flag.

        Prevent self ECU from rebooting when their is at least one ECU
        under UPDATING ota_status.
        """
        while not _otaclient_shutdown:
            can_reboot_flag = self._reboot_flag.is_set()

            local_ota_status = self._ecu_status_storage.get_self_status()
            if local_ota_status is None:
                await self._polling_waiter()
                continue

            _sub_ecus_ok = not self._ecu_status_storage.in_update_child_ecus_id
            # wait for all stats being collected by the collector
            _local_ecu_ok = not (
                local_ota_status.ota_status == api_types.StatusOta.UPDATING
                and local_ota_status.update_status.phase
                != api_types.UpdatePhase.FINALIZING_UPDATE
            )

            if _sub_ecus_ok and _local_ecu_ok:
                if not can_reboot_flag:
                    logger.info(
                        "local otaclient can reboot as no child ECU and/or local ECU is in UPDATING ota_status"
                    )
                self._reboot_flag.set()
            else:
                if can_reboot_flag:
                    logger.info(
                        f"local otaclient cannot reboot as child ECUs {self._ecu_status_storage.in_update_child_ecus_id}"
                        " are in UPDATING ota_status and/or local OTA is not yet finished"
                    )
                self._reboot_flag.clear()
            await self._polling_waiter()

    async def _local_update(
        self, request: api_types.UpdateRequestEcu
    ) -> api_types.UpdateResponseEcu:
        # compose request
        internal_req = UpdateRequestV2(
            version=request.version,
            url_base=request.url,
            cookies_json=request.cookies,
        )
        self._operation_push_queue.put_nowait(internal_req)

        try:
            resp = await self._run_in_executor(
                self._operation_ack_queue.get, True, _MAX_WAIT_RESP_TIME
            )
        except Exception:
            logger.error("timeout wait for the resp, THIS SHOULD NOT HAPPEND!")
            return api_types.UpdateResponseEcu(
                ecu_id=self.my_ecu_id,
                result=api_types.FailureType.UNRECOVERABLE,
            )

        if resp == OTAOperationResp.BUSY:
            logger.warning("local otaclient is busy, ignore request")
            return api_types.UpdateResponseEcu(
                ecu_id=self.my_ecu_id,
                result=api_types.FailureType.RECOVERABLE,
            )

        logger.info("local OTA update dispatched")
        return api_types.UpdateResponseEcu(
            ecu_id=self.my_ecu_id,
            result=api_types.FailureType.NO_FAILURE,
        )

    async def _local_rollback(
        self, _: api_types.RollbackRequestEcu
    ) -> api_types.RollbackResponseEcu:
        self._operation_push_queue.put_nowait(RollbackRequestV2())
        try:
            resp = await self._run_in_executor(
                self._operation_ack_queue.get, True, _MAX_WAIT_RESP_TIME
            )
        except Exception:
            logger.error("timeout wait for the resp, THIS SHOULD NOT HAPPEND!")
            return api_types.RollbackResponseEcu(
                ecu_id=self.my_ecu_id,
                result=api_types.FailureType.UNRECOVERABLE,
            )

        if resp == OTAOperationResp.BUSY:
            logger.warning("local otaclient is busy, ignore request")
            return api_types.RollbackResponseEcu(
                ecu_id=self.my_ecu_id,
                result=api_types.FailureType.RECOVERABLE,
            )

        logger.info("local OTA rollback dispatched")
        return api_types.RollbackResponseEcu(
            ecu_id=self.my_ecu_id,
            result=api_types.FailureType.NO_FAILURE,
        )

    # API implementation

    async def update_handler(
        self, request: api_types.UpdateRequest
    ) -> api_types.UpdateResponse:
        logger.info(f"receive update request: {request}")
        update_acked_ecus = set()
        response = api_types.UpdateResponse()

        # first: dispatch update request to all directly connected subECUs
        tasks: dict[asyncio.Task, ECUContact] = {}
        for ecu_contact in self.sub_ecus:
            if not request.if_contains_ecu(ecu_contact.ecu_id):
                continue
            _task = asyncio.create_task(
                OTAClientCall.update_call(
                    ecu_contact.ecu_id,
                    str(ecu_contact.ip_addr),
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
                    _ecu_resp: api_types.UpdateResponse = _task.result()
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
                        api_types.UpdateResponseEcu(
                            ecu_id=_ecu_contact.ecu_id,
                            result=api_types.FailureType.RECOVERABLE,
                        )
                    )
            tasks.clear()

        # second: dispatch update request to local if required by incoming request
        if update_req_ecu := request.find_ecu(self.my_ecu_id):
            _resp_ecu = await self._local_update(update_req_ecu)
            response.add_ecu(_resp_ecu)

            if _resp_ecu.result == api_types.FailureType.NO_FAILURE:
                update_acked_ecus.add(self.my_ecu_id)

        # finally, trigger ecu_status_storage entering active mode if needed
        if update_acked_ecus:
            logger.info(f"ECUs accept OTA request: {update_acked_ecus}")
            asyncio.create_task(
                self._ecu_status_storage.on_ecus_accept_update_request(
                    update_acked_ecus
                )
            )

        return response

    async def rollback_handler(
        self, request: api_types.RollbackRequest
    ) -> api_types.RollbackResponse:
        logger.info(f"receive rollback request: {request}")
        response = api_types.RollbackResponse()

        # first: dispatch rollback request to all directly connected subECUs
        tasks: dict[asyncio.Task, ECUContact] = {}
        for ecu_contact in self.sub_ecus:
            if not request.if_contains_ecu(ecu_contact.ecu_id):
                continue
            _task = asyncio.create_task(
                OTAClientCall.rollback_call(
                    ecu_contact.ecu_id,
                    str(ecu_contact.ip_addr),
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
                    _ecu_resp: api_types.RollbackResponse = _task.result()
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
                        api_types.RollbackResponseEcu(
                            ecu_id=_ecu_contact.ecu_id,
                            result=api_types.FailureType.RECOVERABLE,
                        )
                    )
            tasks.clear()

        # second: dispatch rollback request to local if required
        if rollback_req := request.find_ecu(self.my_ecu_id):
            _resp_ecu = await self._local_rollback(rollback_req)
            response.add_ecu(_resp_ecu)
        return response

    async def status_handler(self, _=None) -> api_types.StatusResponse:
        return await self._ecu_status_storage.export()


class OTAClientAPIServicer(_OTAClientAPIServicer, pb2_grpc.OtaClientServiceServicer):

    async def Update(
        self, request: pb2.UpdateRequest, context
    ) -> pb2.UpdateResponse:  # pragma: no cover
        response = await self.update_handler(api_types.UpdateRequest.convert(request))
        return response.export_pb()

    async def Rollback(
        self, request: pb2.RollbackRequest, context
    ) -> pb2.RollbackResponse:  # pragma: no cover
        response = await self.rollback_handler(
            api_types.RollbackRequest.convert(request)
        )
        return response.export_pb()

    async def Status(
        self, request: pb2.StatusRequest, context
    ) -> pb2.StatusResponse:  # pragma: no cover
        response = await self.status_handler(api_types.StatusRequest.convert(request))
        return response.export_pb()
