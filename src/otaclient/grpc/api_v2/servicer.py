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
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, overload

import otaclient.configs.cfg as otaclient_cfg
from otaclient._types import (
    AbortOTAFlag,
    AbortRequestV2,
    ClientUpdateRequestV2,
    CriticalZoneFlag,
    IPCRequest,
    IPCResEnum,
    IPCResponse,
    OTAStatus,
    RollbackRequestV2,
    UpdateRequestV2,
)
from otaclient._utils import SharedOTAClientStatusReader, gen_request_id, gen_session_id
from otaclient.configs import ECUContact
from otaclient.configs.cfg import cfg, ecu_info
from otaclient.grpc.api_v2.ecu_status import ECUStatusStorage
from otaclient_api.v2 import _types as api_types
from otaclient_api.v2.api_caller import (
    ECUAbortNotSupported,
    ECUNoResponse,
    OTAClientCall,
)

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
        critical_zone_flag: CriticalZoneFlag,
        abort_ota_flag: AbortOTAFlag,
        shm_reader: SharedOTAClientStatusReader,
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
        self._critical_zone_flag = critical_zone_flag
        self._abort_ota_flag = abort_ota_flag
        self._shm_reader = shm_reader
        self._abort_queued_lock = threading.Lock()
        self._polling_waiter = self._ecu_status_storage.get_polling_waiter()

    def _local_update(self, request: UpdateRequestV2) -> api_types.UpdateResponseEcu:
        """Thread worker for dispatching a local update."""
        return self._dispatch_local_request(request, api_types.UpdateResponseEcu)

    def _local_client_update(
        self, request: ClientUpdateRequestV2
    ) -> api_types.ClientUpdateResponseEcu:
        """Thread worker for dispatching a local client update."""
        return self._dispatch_local_request(request, api_types.ClientUpdateResponseEcu)

    @overload
    def _dispatch_local_request(
        self,
        request: UpdateRequestV2,
        response_type: type[api_types.UpdateResponseEcu],
    ) -> api_types.UpdateResponseEcu: ...

    @overload
    def _dispatch_local_request(
        self,
        request: ClientUpdateRequestV2,
        response_type: type[api_types.ClientUpdateResponseEcu],
    ) -> api_types.ClientUpdateResponseEcu: ...

    def _dispatch_local_request(
        self,
        request: UpdateRequestV2 | ClientUpdateRequestV2,
        response_type: (
            type[api_types.UpdateResponseEcu] | type[api_types.ClientUpdateResponseEcu]
        ),
    ) -> api_types.UpdateResponseEcu | api_types.ClientUpdateResponseEcu:
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

    @staticmethod
    def _add_ecu_into_response(
        response: (
            api_types.UpdateResponse
            | api_types.RollbackResponse
            | api_types.ClientUpdateResponse
        ),
        ecu_id: str,
        failure_type: api_types.FailureType,
    ) -> None:
        """Add ECU into response with specified failure type."""
        if isinstance(response, api_types.UpdateResponse):
            ecu_response = api_types.UpdateResponseEcu(
                ecu_id=ecu_id,
                result=failure_type,
            )
            response.add_ecu(ecu_response)
        elif isinstance(response, api_types.RollbackResponse):
            ecu_response = api_types.RollbackResponseEcu(
                ecu_id=ecu_id,
                result=failure_type,
            )
            response.add_ecu(ecu_response)
        elif isinstance(response, api_types.ClientUpdateResponse):
            ecu_response = api_types.ClientUpdateResponseEcu(
                ecu_id=ecu_id,
                result=failure_type,
            )
            response.add_ecu(ecu_response)
        else:
            raise ValueError(
                "invalid response type: "
                f"{type(response).__name__}; expected one of: "
                "UpdateResponse, RollbackResponse, ClientUpdateResponse"
            )

    @staticmethod
    def _add_ecu_into_abort_response(
        response: api_types.AbortResponse,
        ecu_id: str,
        abort_failure_type: api_types.AbortFailureType,
        message: str = "",
    ) -> None:
        """Add ECU into abort response with specified failure type."""
        ecu_response = api_types.AbortResponseEcu(
            ecu_id=ecu_id,
            result=abort_failure_type,
            message=message,
        )
        response.add_ecu(ecu_response)

    @staticmethod
    def _create_local_request(
        req_ecu: api_types.UpdateRequestEcu | api_types.ClientUpdateRequestEcu,
        request_cls: (
            type[UpdateRequestV2]
            | type[RollbackRequestV2]
            | type[ClientUpdateRequestV2]
        ),
        request_id: str,
    ) -> UpdateRequestV2 | RollbackRequestV2 | ClientUpdateRequestV2:
        if (
            isinstance(req_ecu, api_types.UpdateRequestEcu)
            and (request_cls is UpdateRequestV2)
        ) or (
            isinstance(req_ecu, api_types.ClientUpdateRequestEcu)
            and (request_cls is ClientUpdateRequestV2)
        ):
            # update or client update
            new_session_id = gen_session_id(req_ecu.version)
            local_request = request_cls(
                version=req_ecu.version,
                url_base=req_ecu.url,
                cookies_json=req_ecu.cookies,
                request_id=request_id,
                session_id=new_session_id,
            )
        elif isinstance(req_ecu, api_types.RollbackRequestEcu) and (
            request_cls is RollbackRequestV2
        ):
            # rollback
            new_session_id = gen_session_id("__rollback")
            local_request = request_cls(
                request_id=request_id, session_id=new_session_id
            )
        else:
            raise ValueError(
                "invalid req_ecu and request_cls combination: {req_ecu}, {request_cls}"
            )

        return local_request

    @overload
    async def _handle_update_request(
        self,
        request: api_types.UpdateRequest,
        local_handler: Callable,
        request_cls: type[UpdateRequestV2],
        remote_call: Callable,
        response_type: type[api_types.UpdateResponse],
        update_acked_ecus: set[str],
    ) -> api_types.UpdateResponse: ...

    @overload
    async def _handle_update_request(
        self,
        request: api_types.ClientUpdateRequest,
        local_handler: Callable,
        request_cls: type[ClientUpdateRequestV2],
        remote_call: Callable,
        response_type: type[api_types.ClientUpdateResponse],
        update_acked_ecus: set[str],
    ) -> api_types.ClientUpdateResponse: ...

    async def _handle_update_request(
        self,
        request: api_types.UpdateRequest | api_types.ClientUpdateRequest,
        local_handler: Callable,
        request_cls: type[UpdateRequestV2] | type[ClientUpdateRequestV2],
        remote_call: Callable,
        response_type: (
            type[api_types.UpdateResponse] | type[api_types.ClientUpdateResponse]
        ),
        update_acked_ecus: set[str] | None,
    ) -> api_types.UpdateResponse | api_types.ClientUpdateResponse:
        """Handle incoming request."""
        logger.info(f"receive request: {request}")
        response = response_type()

        if self._abort_ota_flag.shutdown_requested.is_set():
            logger.error(
                "otaclient is stopping due to OTA ABORT requested. Rejecting all further incoming request"
            )
            for _req in request.iter_ecu():
                self._add_ecu_into_response(
                    response, _req.ecu_id, api_types.FailureType.UNRECOVERABLE
                )
            return response

        if not request.request_id:
            request.request_id = gen_request_id()

        # NOTE(20241220): due to the fact that OTA Service API doesn't have field
        #                 in UpdateResponseEcu msg, the only way to pass the failure_msg
        #                 to upper is by status API.
        if not otaclient_cfg.ECU_INFO_LOADED_SUCCESSFULLY:
            logger.error("ecu_info.yaml is not loaded properly, reject any request")
            for _req in request.iter_ecu():
                self._add_ecu_into_response(
                    response, _req.ecu_id, api_types.FailureType.UNRECOVERABLE
                )
            return response
        # first: dispatch update request to all directly connected sub ECUs
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
                    if update_acked_ecus is not None:
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
                    self._add_ecu_into_response(
                        response,
                        _ecu_contact.ecu_id,
                        api_types.FailureType.RECOVERABLE,
                    )
            tasks.clear()

        # second: dispatch update request to local if required by incoming request
        if req_ecu := request.find_ecu(self.my_ecu_id):
            local_request = self._create_local_request(
                req_ecu=req_ecu, request_cls=request_cls, request_id=request.request_id
            )
            _resp = await asyncio.get_running_loop().run_in_executor(
                self._executor,
                local_handler,
                local_request,
            )

            if (
                update_acked_ecus is not None
                and _resp.result == api_types.FailureType.NO_FAILURE
            ):
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

    def _is_abort_rejected_by_final_phase(self, caller: str = "") -> bool:
        """Check if abort should be rejected because OTA is in final phase.

        Args:
            caller: Optional identifier for the caller context (for logging).

        Returns:
            True if abort should be rejected (OTA in final phase), False otherwise.
        """
        if self._abort_ota_flag.reject_abort.is_set():
            caller_info = f" [{caller}]" if caller else ""
            logger.info(
                f"abort request rejected{caller_info}: OTA update is in final phase "
                "(post_update/finalize_update)"
            )
            return True
        return False

    def _process_queued_abort(self) -> None:
        try:
            # Check if OTA entered final phase while we were waiting
            if self._is_abort_rejected_by_final_phase("before_critical_zone_lock"):
                return

            with self._critical_zone_flag.acquire_lock_with_release(blocking=True):
                # Double-check reject_abort after acquiring lock
                if self._is_abort_rejected_by_final_phase("after_critical_zone_lock"):
                    return

                logger.warning(
                    "critical zone ended, processing queued abort request..."
                )
                self._abort_ota_flag.shutdown_requested.set()
                logger.info("Abort OTA flag is set properly.")
        finally:
            # Guard against releasing an unlocked lock in case this method is
            # ever called without acquiring _abort_queued_lock beforehand.
            if self._abort_queued_lock.locked():
                self._abort_queued_lock.release()

    def _handle_abort_request(
        self, request: AbortRequestV2
    ) -> api_types.AbortResponseEcu:
        """Dispatch abort request to main process."""
        logger.info(f"handling request: {request}")
        if not isinstance(request, AbortRequestV2):
            return api_types.AbortResponseEcu(
                ecu_id="",
                result=api_types.AbortFailureType.ABORT_FAILURE,
                message="invalid abort request",
            )

        try:
            if self._abort_ota_flag.shutdown_requested.is_set():
                return api_types.AbortResponseEcu(
                    ecu_id=self.my_ecu_id,
                    result=api_types.AbortFailureType.ABORT_NO_FAILURE,
                    message="Abort already in progress",
                )

            # Check if there's an active OTA update to abort
            _local_status = self._shm_reader.sync_msg()
            if _local_status is None:
                logger.info(
                    "abort request rejected: no active OTA update (current status: unknown)"
                )
                return api_types.AbortResponseEcu(
                    ecu_id=self.my_ecu_id,
                    result=api_types.AbortFailureType.ABORT_FAILURE,
                    message="Cannot abort: no active OTA update in progress",
                )

            if _local_status.ota_status not in (
                OTAStatus.UPDATING,
                OTAStatus.CLIENT_UPDATING,
            ):
                logger.info(
                    f"abort request rejected: no active OTA update "
                    f"(current status: {_local_status.ota_status})"
                )
                return api_types.AbortResponseEcu(
                    ecu_id=self.my_ecu_id,
                    result=api_types.AbortFailureType.ABORT_FAILURE,
                    message="Cannot abort: no active OTA update in progress",
                )

            # Check if we're in final update phases (post_update/finalize_update)
            # where abort should be rejected, not queued
            if self._is_abort_rejected_by_final_phase("handle_abort_request"):
                return api_types.AbortResponseEcu(
                    ecu_id=self.my_ecu_id,
                    result=api_types.AbortFailureType.ABORT_FAILURE,
                    message="Cannot abort: OTA update is in final phase and will complete shortly",
                )

            with self._critical_zone_flag.acquire_lock_with_release(
                blocking=False
            ) as _lock_acquired:
                if _lock_acquired:
                    # Double-check reject_abort after acquiring lock to handle race condition
                    # where OTA entered final phase between the initial check and lock acquisition
                    if self._is_abort_rejected_by_final_phase(
                        "handle_abort_request_after_lock"
                    ):
                        return api_types.AbortResponseEcu(
                            ecu_id=self.my_ecu_id,
                            result=api_types.AbortFailureType.ABORT_FAILURE,
                            message="Cannot abort: OTA update is in final phase and will complete shortly",
                        )

                    # Lock acquired = NOT in critical zone, process immediately
                    logger.warning(
                        "abort function requested, interrupting OTA and exit now ..."
                    )
                    self._abort_ota_flag.shutdown_requested.set()
                    logger.info("Abort OTA flag is set properly.")
                    return api_types.AbortResponseEcu(
                        ecu_id=self.my_ecu_id,
                        result=api_types.AbortFailureType.ABORT_NO_FAILURE,
                    )
                else:
                    # Lock not acquired = IN critical zone, queue abort
                    # Only spawn one thread to wait for critical zone to end
                    # acquire(blocking=False) is an atomic test-and-set gate
                    if not self._abort_queued_lock.acquire(blocking=False):
                        logger.info("abort already queued, skipping duplicate")
                    else:
                        logger.warning("in critical zone, queuing abort request...")
                        threading.Thread(
                            target=self._process_queued_abort,
                            daemon=True,
                        ).start()
                    return api_types.AbortResponseEcu(
                        ecu_id=self.my_ecu_id,
                        result=api_types.AbortFailureType.ABORT_NO_FAILURE,
                        message="Abort request queued, will process after critical zone",
                    )

        except Exception as e:
            logger.error(f"failed to send abort request to main process: {e!r}")
            return api_types.AbortResponseEcu(
                ecu_id=self.my_ecu_id,
                result=api_types.AbortFailureType.ABORT_FAILURE,
                message="Failed to process abort request",
            )

    # API methods

    async def update(
        self, request: api_types.UpdateRequest
    ) -> api_types.UpdateResponse:
        return await self._handle_update_request(
            request=request,
            local_handler=self._local_update,
            request_cls=UpdateRequestV2,
            remote_call=OTAClientCall.update_call,
            response_type=api_types.UpdateResponse,
            update_acked_ecus=set(),
        )

    async def abort(self, request: api_types.AbortRequest) -> api_types.AbortResponse:
        """Handle abort request for local and sub-ECUs."""
        logger.info(f"receive abort request: {request}")
        response = api_types.AbortResponse()

        if not request.request_id:
            request.request_id = gen_request_id()

        # First: dispatch abort request to all directly connected sub-ECUs
        # NOTE: v1 aborts all ECUs unconditionally (no per-ECU filtering)
        tasks: dict[asyncio.Task, ECUContact] = {}
        for ecu_contact in self.sub_ecus:
            _task = asyncio.create_task(
                OTAClientCall.abort_call(
                    ecu_contact.ecu_id,
                    str(ecu_contact.ip_addr),
                    ecu_contact.port,
                    request=request,
                    timeout=cfg.WAITING_SUBECU_ACK_REQ_TIMEOUT,
                )
            )
            tasks[_task] = ecu_contact

        if tasks:
            done, _ = await asyncio.wait(tasks)
            for _task in done:
                try:
                    _ecu_resp = _task.result()
                    _ecu_contact = tasks[_task]
                    logger.info(f"{_ecu_contact} abort response: {_ecu_resp}")
                    response.merge_from(_ecu_resp)
                except ECUAbortNotSupported:
                    _ecu_contact = tasks[_task]
                    logger.warning(
                        f"{_ecu_contact} does not support the abort endpoint"
                        " (older OTA Client version without abort support)"
                    )
                    self._add_ecu_into_abort_response(
                        response,
                        _ecu_contact.ecu_id,
                        api_types.AbortFailureType.ABORT_FAILURE,
                        message=(
                            f"ECU {_ecu_contact.ecu_id} does not support"
                            " the abort endpoint (UNIMPLEMENTED)"
                        ),
                    )
                except ECUNoResponse as e:
                    _ecu_contact = tasks[_task]
                    logger.error(
                        f"{_ecu_contact} did not respond to abort request on-time"
                        f"(within {cfg.WAITING_SUBECU_ACK_REQ_TIMEOUT}s): {e!r}"
                    )
                    self._add_ecu_into_abort_response(
                        response,
                        _ecu_contact.ecu_id,
                        api_types.AbortFailureType.ABORT_FAILURE,
                        message=str(e),
                    )
            tasks.clear()

        # Second: handle local ECU abort request
        new_session_id = gen_session_id("__abort")
        local_abort_request = AbortRequestV2(
            request_id=request.request_id,
            session_id=new_session_id,
        )
        local_response = self._handle_abort_request(local_abort_request)
        response.add_ecu(local_response)

        return response

    async def rollback(
        self, request: api_types.RollbackRequest
    ) -> api_types.RollbackResponse:
        # NOTE(20250818): remove rollback API handler support
        _res = []
        for _ecu_req in request.ecu:
            _res.append(
                api_types.RollbackResponseEcu(
                    ecu_id=_ecu_req.ecu_id,
                    result=api_types.FailureType.RECOVERABLE,
                    message="rollback API support is removed",
                ),
            )
        return api_types.RollbackResponse(ecu=_res)

    async def client_update(
        self, request: api_types.ClientUpdateRequest
    ) -> api_types.ClientUpdateResponse:
        return await self._handle_update_request(
            request=request,
            local_handler=self._local_client_update,
            request_cls=ClientUpdateRequestV2,
            remote_call=OTAClientCall.client_update_call,
            response_type=api_types.ClientUpdateResponse,
            update_acked_ecus=set(),
        )

    async def status(self, _=None) -> api_types.StatusResponse:
        return await self._ecu_status_storage.export()
