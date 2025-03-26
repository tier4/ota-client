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
from concurrent.futures import ThreadPoolExecutor

import otaclient.configs.cfg as otaclient_cfg
from otaclient._types import (
    IPCRequest,
    IPCResEnum,
    IPCResponse,
    RollbackRequestV2,
    UpdateRequestV2,
)
from otaclient._utils import gen_session_id
from otaclient.configs import ECUContact
from otaclient.configs.cfg import cfg, ecu_info
from otaclient.grpc.api_v2.ecu_status import ECUStatusStorage
from otaclient_api.v2 import _types as api_types
from otaclient_api.v2.api_caller import ECUNoResponse, OTAClientCall

logger = logging.getLogger(__name__)

WAIT_FOR_LOCAL_ECU_ACK_TIMEOUT = 6  # seconds


class OTAClientAPIServicer:
    """Handlers for otaclient service API.

    This class also handles otaproxy lifecycle and dependency management.
    """

    def __init__(
        self,
        *,
        ecu_status_storage: ECUStatusStorage,
        op_queue: mp_queue.Queue[IPCRequest],
        resp_queue: mp_queue.Queue[IPCResponse],
        executor: ThreadPoolExecutor,
    ):
        self.sub_ecus = ecu_info.secondaries
        self.listen_addr = ecu_info.ip_addr
        self.listen_port = cfg.OTA_API_SERVER_PORT
        self.my_ecu_id = ecu_info.ecu_id
        self._executor = executor

        self._op_queue = op_queue
        self._resp_queue = resp_queue

        self._ecu_status_storage = ecu_status_storage
        self._polling_waiter = self._ecu_status_storage.get_polling_waiter()

    def _local_update(self, request: UpdateRequestV2) -> api_types.UpdateResponseEcu:
        """Thread worker for dispatching a local update."""
        return self._dispatch_local_request(request, api_types.UpdateResponseEcu)

    def _local_rollback(
        self, rollback_request: RollbackRequestV2
    ) -> api_types.RollbackResponseEcu:
        """Thread worker for dispatching a local rollback."""
        return self._dispatch_local_request(
            rollback_request, api_types.RollbackResponseEcu
        )

    def _dispatch_local_request(self, request, response_type):
        self._op_queue.put_nowait(request)
        try:
            _req_response = self._resp_queue.get(timeout=WAIT_FOR_LOCAL_ECU_ACK_TIMEOUT)
            assert isinstance(_req_response, IPCResponse), "unexpected msg"
            assert (
                _req_response.session_id == request.session_id
            ), "mismatched session_id"

            if _req_response.res == IPCResEnum.ACCEPT:
                return response_type(
                    ecu_id=self.my_ecu_id,
                    result=api_types.FailureType.NO_FAILURE,
                )
            else:
                logger.error(
                    f"local otaclient doesn't accept request: {_req_response.msg}"
                )
                return response_type(
                    ecu_id=self.my_ecu_id,
                    result=api_types.FailureType.RECOVERABLE,
                )
        except AssertionError as e:
            logger.error(f"local otaclient response with unexpected msg: {e!r}")
            return response_type(
                ecu_id=self.my_ecu_id,
                result=api_types.FailureType.RECOVERABLE,
            )
        except Exception as e:  # failed to get ACK from otaclient within timeout
            logger.error(f"local otaclient failed to ACK request: {e!r}")
            return response_type(
                ecu_id=self.my_ecu_id,
                result=api_types.FailureType.UNRECOVERABLE,
            )

    async def _handle_request(
        self,
        request,
        local_handler,
        request_cls,
        remote_call,
        response_type,
        response_ecu_type,
    ):
        logger.info(f"receive request: {request}")
        update_acked_ecus = set()
        response = response_type()

        if not otaclient_cfg.ECU_INFO_LOADED_SUCCESSFULLY:
            logger.error("ecu_info.yaml is not loaded properly, reject any request")
            for _req in request.iter_ecu():
                response.add_ecu(
                    response_ecu_type(
                        ecu_id=_req.ecu_id,
                        result=api_types.FailureType.UNRECOVERABLE,
                    )
                )
            return response

        # first: dispatch update request to all directly connected subECUs
        tasks: dict[asyncio.Task, ECUContact] = {}
        for ecu_contact in self.sub_ecus:
            if not request.if_contains_ecu(ecu_contact.ecu_id):
                continue
            _task = asyncio.create_task(
                remote_call(
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
                    _ecu_resp = _task.result()
                    update_acked_ecus.update(_ecu_resp.ecus_acked_update)
                    response.merge_from(_ecu_resp)
                except ECUNoResponse as e:
                    _ecu_contact = tasks[_task]
                    logger.warning(
                        f"{_ecu_contact} doesn't respond to request on-time"
                        f"(within {cfg.WAITING_SUBECU_ACK_REQ_TIMEOUT}s): {e!r}"
                    )
                    # NOTE(20230517): aligns with the previous behavior that create
                    #                 response with RECOVERABLE OTA error for unresponsive
                    #                 ECU.
                    response.add_ecu(
                        response_ecu_type(
                            ecu_id=_ecu_contact.ecu_id,
                            result=api_types.FailureType.RECOVERABLE,
                        )
                    )
            tasks.clear()

        # second: dispatch update request to local if required by incoming request
        if update_req_ecu := request.find_ecu(self.my_ecu_id):
            new_session_id = gen_session_id(update_req_ecu.version)
            if request_cls == RollbackRequestV2:
                local_request = request_cls(session_id=new_session_id)
            else:
                local_request = request_cls(
                    version=update_req_ecu.version,
                    url_base=update_req_ecu.url,
                    cookies_json=update_req_ecu.cookies,
                    session_id=new_session_id,
                )
            _resp = await asyncio.get_running_loop().run_in_executor(
                self._executor,
                local_handler,
                local_request,
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

    # API methods

    async def update(
        self, request: api_types.UpdateRequest
    ) -> api_types.UpdateResponse:
        return await self._handle_request(
            request,
            self._local_update,
            UpdateRequestV2,
            OTAClientCall.update_call,
            api_types.UpdateResponse,
            api_types.UpdateResponseEcu,
        )

    async def rollback(
        self, request: api_types.RollbackRequest
    ) -> api_types.RollbackResponse:
        return await self._handle_request(
            request,
            self._local_rollback,
            RollbackRequestV2,
            OTAClientCall.rollback_call,
            api_types.RollbackResponse,
            api_types.RollbackResponseEcu,
        )

    async def status(self, _=None) -> api_types.StatusResponse:
        return await self._ecu_status_storage.export()
