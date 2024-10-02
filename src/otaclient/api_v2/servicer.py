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
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from multiprocessing.queues import Queue as mp_Queue

import otaclient_api.v2.otaclient_v2_pb2 as pb2
import otaclient_api.v2.otaclient_v2_pb2_grpc as pb2_grpc
import otaclient_api.v2.types as api_types
from otaclient._types import (
    OTAOperationResp,
    RollbackRequestV2,
    UpdateRequestV2,
)
from otaclient.api_v2.ecu_status import ECUStatusStorage
from otaclient.app.configs import ecu_info, server_cfg
from otaclient.configs.ecu_info import ECUContact
from otaclient_api.v2.api_caller import ECUNoResponse, OTAClientCall

logger = logging.getLogger(__name__)

_otaclient_shutdown = False
_ota_operation_ack_q: mp_Queue | None = None


def _global_shutdown():  # pragma: no cover
    global _otaclient_shutdown
    _otaclient_shutdown = True

    if _ota_operation_ack_q:
        _ota_operation_ack_q.put_nowait(None)


atexit.register(_global_shutdown)


OTA_SESSION_LOCK_CHECK_INTERVAL = 6
OTA_SESSION_LOCK_MINIMUM_OTA_TIME = 60
_MAX_WAIT_RESP_TIME = 10


class _OTAClientAPIServicer:
    """Handlers for otaclient service API.

    This class also handles otaproxy lifecyle and dependence managing.
    """

    def __init__(
        self,
        *,
        ecu_status_storage: ECUStatusStorage,
        operation_push_queue: mp_Queue,
        operation_ack_queue: mp_Queue,
    ):
        # NOTE: normally we should handle one OTA request at a time, and
        #   serializing the request dispatching.
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="api_servicer_threadpool"
        )
        self._run_in_executor = partial(
            asyncio.get_running_loop().run_in_executor, self._executor
        )
        self._operation_push_queue = operation_push_queue
        self._operation_ack_queue = operation_ack_queue

        global _ota_operation_ack_q
        _ota_operation_ack_q = operation_ack_queue

        self.sub_ecus = ecu_info.secondaries
        self.listen_addr = ecu_info.ip_addr
        self.listen_port = server_cfg.SERVER_PORT
        self.my_ecu_id = ecu_info.ecu_id

        # start ecu status tracking
        self._ecu_status_storage = ecu_status_storage

    # internal

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
            logger.warning("timeout wait for the resp from the local otaclient!")
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
            logger.warning("timeout wait for the resp from the local otaclient!")
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
