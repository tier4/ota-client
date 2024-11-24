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
import multiprocessing.queues as mp_queue
import multiprocessing.synchronize as mp_sync

from otaclient._types import (
    IPCRequest,
    IPCResEnum,
    IPCResponse,
    RollbackRequestV2,
    UpdateRequestV2,
)
from otaclient._utils import gen_session_id
from otaclient.configs import ECUContact
from otaclient.configs.cfg import cfg, ecu_info, proxy_info
from otaclient.grpc.api_v2.ecu_status import ECUStatusStorage
from otaclient_api.v2 import types as api_types
from otaclient_api.v2.api_caller import ECUNoResponse, OTAClientCall

logger = logging.getLogger(__name__)

WAIT_FOR_ACK_TIMEOUT = 6  # seconds


class OTAClientAPIServicer:
    """Handlers for otaclient service API.

    This class also handles otaproxy lifecyle and dependence managing.
    """

    OTAPROXY_SHUTDOWN_DELAY = cfg.OTAPROXY_MINIMUM_SHUTDOWN_INTERVAL

    def __init__(
        self,
        ecu_status_storage: ECUStatusStorage,
        op_queue: mp_queue.Queue[IPCRequest],
        resp_queue: mp_queue.Queue[IPCResponse],
        *,
        control_flag: mp_sync.Event,
    ):
        self.sub_ecus = ecu_info.secondaries
        self.listen_addr = ecu_info.ip_addr
        self.listen_port = cfg.OTA_API_SERVER_PORT
        self.my_ecu_id = ecu_info.ecu_id

        self._otaclient_control_flag = control_flag
        self._op_queue = op_queue
        self._resp_queue = resp_queue

        self._ecu_status_storage = ecu_status_storage
        self._polling_waiter = self._ecu_status_storage.get_polling_waiter()

        # otaproxy lifecycle and dependency managing
        # NOTE: _debug_status_checking_shutdown_event is for test only,
        #       allow us to stop background task without changing codes.
        #       In normal running this event will never be set.
        self._debug_status_checking_shutdown_event = asyncio.Event()
        if proxy_info.enable_local_ota_proxy:
            asyncio.create_task(self._otaclient_control_flag_managing())
        else:
            # if otaproxy is not enabled, no dependency relationship will be formed,
            # always allow local otaclient to reboot
            self._otaclient_control_flag.set()

    # internal

    async def _otaclient_control_flag_managing(self):
        """Task entry for set/clear otaclient control flags.

        Prevent self ECU from rebooting when their is at least one ECU
        under UPDATING ota_status.
        """
        while not self._debug_status_checking_shutdown_event.is_set():
            _can_reboot = self._otaclient_control_flag.is_set()
            if not self._ecu_status_storage.in_update_child_ecus_id:
                if not _can_reboot:
                    logger.info(
                        "local otaclient can reboot as no child ECU is in UPDATING ota_status"
                    )
                self._otaclient_control_flag.set()
            else:
                if _can_reboot:
                    logger.info(
                        f"local otaclient cannot reboot as child ECUs {self._ecu_status_storage.in_update_child_ecus_id}"
                        " are in UPDATING ota_status"
                    )
                self._otaclient_control_flag.clear()
            await self._polling_waiter()

    # API servicer

    def _local_update(self, request: UpdateRequestV2) -> api_types.UpdateResponseEcu:
        self._op_queue.put_nowait(request)
        try:
            _req_response = self._resp_queue.get(timeout=WAIT_FOR_ACK_TIMEOUT)
            assert isinstance(_req_response, IPCResponse), "unexpected msg"
            assert (
                _req_response.session_id == request.session_id
            ), "mismatched session_id"

            if _req_response.res == IPCResEnum.ACCEPT:
                return api_types.UpdateResponseEcu(
                    ecu_id=self.my_ecu_id,
                    result=api_types.FailureType.NO_FAILURE,
                )
            else:
                logger.error(
                    f"local otaclient doesn't accept upate request: {_req_response.msg}"
                )
                return api_types.UpdateResponseEcu(
                    ecu_id=self.my_ecu_id,
                    result=api_types.FailureType.RECOVERABLE,
                )
        except AssertionError as e:
            logger.error(f"local otaclient response with unexpected msg: {e!r}")
            return api_types.UpdateResponseEcu(
                ecu_id=self.my_ecu_id,
                result=api_types.FailureType.RECOVERABLE,
            )
        except Exception as e:  # failed to get ACK from otaclient within timeout
            logger.error(f"local otaclient failed to ACK request: {e!r}")
            return api_types.UpdateResponseEcu(
                ecu_id=self.my_ecu_id,
                result=api_types.FailureType.UNRECOVERABLE,
            )

    async def update(
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
            new_session_id = gen_session_id(update_req_ecu.version)
            _resp = self._local_update(
                UpdateRequestV2(
                    version=update_req_ecu.version,
                    url_base=update_req_ecu.url,
                    cookies_json=update_req_ecu.cookies,
                    session_id=new_session_id,
                )
            )

            if _resp.result == api_types.FailureType.NO_FAILURE:
                update_acked_ecus.add(self.my_ecu_id)
            response.add_ecu(_resp)

        # finally, trigger ecu_status_storage entering active mode if needed
        if update_acked_ecus:
            logger.info(f"ECUs accept OTA request: {update_acked_ecus}")
            asyncio.create_task(
                self._ecu_status_storage.on_ecus_accept_update_request(
                    update_acked_ecus
                )
            )
        return response

    def _local_rollback(
        self, rollback_request: RollbackRequestV2
    ) -> api_types.RollbackResponseEcu:
        self._op_queue.put_nowait(rollback_request)
        try:
            _req_response = self._resp_queue.get(timeout=WAIT_FOR_ACK_TIMEOUT)
            assert isinstance(
                _req_response, IPCResponse
            ), f"unexpected response: {type(_req_response)}"
            assert (
                _req_response.session_id == rollback_request.session_id
            ), "mismatched session_id"

            if _req_response.res == IPCResEnum.ACCEPT:
                return api_types.RollbackResponseEcu(
                    ecu_id=self.my_ecu_id,
                    result=api_types.FailureType.NO_FAILURE,
                )
            else:
                logger.error(
                    f"local otaclient doesn't accept upate request: {_req_response.msg}"
                )
                return api_types.RollbackResponseEcu(
                    ecu_id=self.my_ecu_id,
                    result=api_types.FailureType.RECOVERABLE,
                )
        except AssertionError as e:
            logger.error(f"local otaclient response with unexpected msg: {e!r}")
            return api_types.RollbackResponseEcu(
                ecu_id=self.my_ecu_id,
                result=api_types.FailureType.RECOVERABLE,
            )
        except Exception as e:  # failed to get ACK from otaclient within timeout
            logger.error(f"local otaclient failed to ACK request: {e!r}")
            return api_types.RollbackResponseEcu(
                ecu_id=self.my_ecu_id,
                result=api_types.FailureType.UNRECOVERABLE,
            )

    async def rollback(
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
        if request.find_ecu(self.my_ecu_id):
            new_session_id = gen_session_id("__rollback")
            response.add_ecu(
                self._local_rollback(RollbackRequestV2(session_id=new_session_id))
            )
        return response

    async def status(self, _=None) -> api_types.StatusResponse:
        return await self._ecu_status_storage.export()
