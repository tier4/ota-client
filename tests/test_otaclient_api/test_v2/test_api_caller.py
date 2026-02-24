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


import grpc
import pytest
import pytest_asyncio

from otaclient_api.v2 import _types as api_types
from otaclient_api.v2 import otaclient_v2_pb2 as v2
from otaclient_api.v2 import otaclient_v2_pb2_grpc as v2_grpc
from otaclient_api.v2.api_caller import (
    ECUAbortNotSupported,
    ECUNoResponse,
    OTAClientCall,
)
from tests.utils import compare_message


class _DummyOTAClientService(v2_grpc.OtaClientServiceServicer):
    DUMMY_STATUS = v2.StatusResponse(
        available_ecu_ids=["autoware", "p1"],
        ecu=[
            v2.StatusResponseEcu(
                ecu_id="autoware",
                result=v2.NO_FAILURE,
                status=v2.Status(
                    status=v2.UPDATING,
                    failure=v2.NO_FAILURE,
                    version="123.x",
                    progress=v2.StatusProgress(
                        phase=v2.REGULAR,
                        total_regular_files=270265,
                        regular_files_processed=270258,
                        files_processed_copy=44315,
                        files_processed_link=144,
                        files_processed_download=225799,
                        file_size_processed_copy=853481625,
                        file_size_processed_link=427019589,
                        file_size_processed_download=24457642270,
                        elapsed_time_copy={"seconds": 15, "nanos": 49000000},
                        elapsed_time_download={"seconds": 416, "nanos": 945000000},
                        total_regular_file_size=25740860425,
                        total_elapsed_time={"seconds": 512, "nanos": 8000000},
                    ),
                ),
            ),
            v2.StatusResponseEcu(
                ecu_id="p1",
                result=v2.NO_FAILURE,
                status=v2.Status(
                    status=v2.SUCCESS,
                    version="789.x",
                ),
            ),
        ],
    )

    DUMMY_UPDATE_REQUEST = v2.UpdateRequest(
        ecu=[
            v2.UpdateRequestEcu(
                ecu_id="autoware",
                version="789.x",
                url="url",
                cookies="cookies",
            ),
            v2.UpdateRequestEcu(
                ecu_id="p1",
                version="789.x",
                url="url",
                cookies="cookies",
            ),
        ]
    )
    DUMMY_UPDATE_RESPONSE = v2.UpdateResponse(
        ecu=[
            v2.UpdateResponseEcu(ecu_id="autoware", result=v2.NO_FAILURE),
            v2.UpdateResponseEcu(ecu_id="p1", result=v2.NO_FAILURE),
        ]
    )
    DUMMY_ROLLBACK_REQUEST = v2.RollbackRequest(
        ecu=[
            v2.RollbackRequestEcu(ecu_id="autoware"),
            v2.RollbackRequestEcu(ecu_id="p1"),
        ]
    )
    DUMMY_ROLLBACK_RESPONSE = v2.RollbackResponse(
        ecu=[
            v2.RollbackResponseEcu(ecu_id="autoware", result=v2.NO_FAILURE),
            v2.RollbackResponseEcu(ecu_id="p1", result=v2.NO_FAILURE),
        ]
    )

    async def Update(self, request: v2.UpdateRequest, context):
        assert request == self.DUMMY_UPDATE_REQUEST
        _res = v2.UpdateResponse()
        _res.CopyFrom(self.DUMMY_UPDATE_RESPONSE)
        return _res

    async def Rollback(self, request: v2.RollbackRequest, context):
        assert request == self.DUMMY_ROLLBACK_REQUEST
        _res = v2.RollbackResponse()
        _res.CopyFrom(self.DUMMY_ROLLBACK_RESPONSE)
        return _res

    async def ClientUpdate(self, request: v2.UpdateRequest, context):
        _res = v2.UpdateResponse()
        _res.CopyFrom(self.DUMMY_UPDATE_RESPONSE)
        return _res

    async def Status(self, request: v2.StatusRequest, context):
        _res = v2.StatusResponse()
        _res.CopyFrom(self.DUMMY_STATUS)
        return _res


DUMMY_ABORT_REQUEST = v2.AbortRequest(ecu=[v2.AbortRequestEcu(ecu_id="autoware")])
DUMMY_ABORT_RESPONSE = v2.AbortResponse(
    ecu=[
        v2.AbortResponseEcu(
            ecu_id="autoware",
            result=v2.ABORT_NO_FAILURE,
        ),
    ]
)


class _SuccessAbortService(v2_grpc.OtaClientServiceServicer):
    """Servicer that successfully handles Abort requests."""

    async def Abort(self, request, context):
        _res = v2.AbortResponse()
        _res.CopyFrom(DUMMY_ABORT_RESPONSE)
        return _res


class _DeadlineExceededAbortService(v2_grpc.OtaClientServiceServicer):
    """Servicer that returns DEADLINE_EXCEEDED for Abort requests."""

    async def Abort(self, request, context):
        await context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, "deadline exceeded")


class _InternalErrorAbortService(v2_grpc.OtaClientServiceServicer):
    """Servicer that returns INTERNAL error for Abort requests."""

    async def Abort(self, request, context):
        await context.abort(grpc.StatusCode.INTERNAL, "internal error")


class TestOTAClientCall:
    OTA_CLIENT_SERVICE_PORT = 50051
    OTA_CLIENT_SERVICE_IP = "127.0.0.1"
    DUMMY_ECU_ID = "autoware"

    @pytest_asyncio.fixture
    async def dummy_ota_client_service(self):
        server = grpc.aio.server()
        v2_grpc.add_OtaClientServiceServicer_to_server(_DummyOTAClientService(), server)
        server.add_insecure_port(
            f"{self.OTA_CLIENT_SERVICE_IP}:{self.OTA_CLIENT_SERVICE_PORT}"
        )
        try:
            await server.start()
            yield
        finally:
            await server.stop(None)

    async def test_update_call(self, dummy_ota_client_service):
        _req = api_types.UpdateRequest.convert(
            _DummyOTAClientService.DUMMY_UPDATE_REQUEST
        )
        _response = await OTAClientCall.update_call(
            ecu_id=self.DUMMY_ECU_ID,
            ecu_ipaddr=self.OTA_CLIENT_SERVICE_IP,
            ecu_port=self.OTA_CLIENT_SERVICE_PORT,
            request=_req,
        )
        compare_message(
            _response.export_pb(), _DummyOTAClientService.DUMMY_UPDATE_RESPONSE
        )

    async def test_rollback_call(self, dummy_ota_client_service):
        _req = api_types.RollbackRequest.convert(
            _DummyOTAClientService.DUMMY_ROLLBACK_REQUEST
        )
        _response = await OTAClientCall.rollback_call(
            ecu_id=self.DUMMY_ECU_ID,
            ecu_ipaddr=self.OTA_CLIENT_SERVICE_IP,
            ecu_port=self.OTA_CLIENT_SERVICE_PORT,
            request=_req,
        )
        compare_message(
            _response.export_pb(), _DummyOTAClientService.DUMMY_ROLLBACK_RESPONSE
        )

    async def test_client_update_call(self, dummy_ota_client_service):
        _req = api_types.ClientUpdateRequest.convert(
            _DummyOTAClientService.DUMMY_UPDATE_REQUEST
        )
        _response = await OTAClientCall.client_update_call(
            ecu_id=self.DUMMY_ECU_ID,
            ecu_ipaddr=self.OTA_CLIENT_SERVICE_IP,
            ecu_port=self.OTA_CLIENT_SERVICE_PORT,
            request=_req,
        )
        compare_message(
            _response.export_pb(), _DummyOTAClientService.DUMMY_UPDATE_RESPONSE
        )

    async def test_status_call(self, dummy_ota_client_service):
        _response = await OTAClientCall.status_call(
            ecu_id=self.DUMMY_ECU_ID,
            ecu_ipaddr=self.OTA_CLIENT_SERVICE_IP,
            ecu_port=self.OTA_CLIENT_SERVICE_PORT,
            request=api_types.StatusRequest(),
        )

        assert _response is not None
        compare_message(_response.export_pb(), _DummyOTAClientService.DUMMY_STATUS)

    async def test_update_call_no_response(self):
        _req = api_types.UpdateRequest.convert(
            _DummyOTAClientService.DUMMY_UPDATE_REQUEST
        )
        with pytest.raises(ECUNoResponse):
            await OTAClientCall.update_call(
                ecu_id=self.DUMMY_ECU_ID,
                ecu_ipaddr=self.OTA_CLIENT_SERVICE_IP,
                ecu_port=self.OTA_CLIENT_SERVICE_PORT,
                request=_req,
                timeout=1,
            )

    async def test_client_update_call_no_response(self):
        _req = api_types.ClientUpdateRequest.convert(
            _DummyOTAClientService.DUMMY_UPDATE_REQUEST
        )
        with pytest.raises(ECUNoResponse):
            await OTAClientCall.client_update_call(
                ecu_id=self.DUMMY_ECU_ID,
                ecu_ipaddr=self.OTA_CLIENT_SERVICE_IP,
                ecu_port=self.OTA_CLIENT_SERVICE_PORT,
                request=_req,
                timeout=1,
            )

    # ------ abort_call tests ------ #

    @pytest_asyncio.fixture
    async def abort_service(self):
        server = grpc.aio.server()
        v2_grpc.add_OtaClientServiceServicer_to_server(_SuccessAbortService(), server)
        server.add_insecure_port(
            f"{self.OTA_CLIENT_SERVICE_IP}:{self.OTA_CLIENT_SERVICE_PORT}"
        )
        try:
            await server.start()
            yield
        finally:
            await server.stop(None)

    @pytest_asyncio.fixture
    async def deadline_exceeded_abort_service(self):
        server = grpc.aio.server()
        v2_grpc.add_OtaClientServiceServicer_to_server(
            _DeadlineExceededAbortService(), server
        )
        server.add_insecure_port(
            f"{self.OTA_CLIENT_SERVICE_IP}:{self.OTA_CLIENT_SERVICE_PORT}"
        )
        try:
            await server.start()
            yield
        finally:
            await server.stop(None)

    @pytest_asyncio.fixture
    async def internal_error_abort_service(self):
        server = grpc.aio.server()
        v2_grpc.add_OtaClientServiceServicer_to_server(
            _InternalErrorAbortService(), server
        )
        server.add_insecure_port(
            f"{self.OTA_CLIENT_SERVICE_IP}:{self.OTA_CLIENT_SERVICE_PORT}"
        )
        try:
            await server.start()
            yield
        finally:
            await server.stop(None)

    async def test_abort_call(self, abort_service):
        _req = api_types.AbortRequest.convert(DUMMY_ABORT_REQUEST)
        _response = await OTAClientCall.abort_call(
            ecu_id=self.DUMMY_ECU_ID,
            ecu_ipaddr=self.OTA_CLIENT_SERVICE_IP,
            ecu_port=self.OTA_CLIENT_SERVICE_PORT,
            request=_req,
        )
        compare_message(_response.export_pb(), DUMMY_ABORT_RESPONSE)

    async def test_abort_call_unimplemented(self, dummy_ota_client_service):
        """UNIMPLEMENTED is raised when the servicer doesn't implement Abort."""
        _req = api_types.AbortRequest.convert(DUMMY_ABORT_REQUEST)
        with pytest.raises(ECUAbortNotSupported):
            await OTAClientCall.abort_call(
                ecu_id=self.DUMMY_ECU_ID,
                ecu_ipaddr=self.OTA_CLIENT_SERVICE_IP,
                ecu_port=self.OTA_CLIENT_SERVICE_PORT,
                request=_req,
            )

    async def test_abort_call_deadline_exceeded(self, deadline_exceeded_abort_service):
        _req = api_types.AbortRequest.convert(DUMMY_ABORT_REQUEST)
        with pytest.raises(
            ECUNoResponse, match="failed to respond to abort request on-time"
        ):
            await OTAClientCall.abort_call(
                ecu_id=self.DUMMY_ECU_ID,
                ecu_ipaddr=self.OTA_CLIENT_SERVICE_IP,
                ecu_port=self.OTA_CLIENT_SERVICE_PORT,
                request=_req,
            )

    async def test_abort_call_grpc_error(self, internal_error_abort_service):
        _req = api_types.AbortRequest.convert(DUMMY_ABORT_REQUEST)
        with pytest.raises(
            ECUNoResponse, match="abort request failed with gRPC error code"
        ):
            await OTAClientCall.abort_call(
                ecu_id=self.DUMMY_ECU_ID,
                ecu_ipaddr=self.OTA_CLIENT_SERVICE_IP,
                ecu_port=self.OTA_CLIENT_SERVICE_PORT,
                request=_req,
            )

    async def test_abort_call_no_response(self):
        """No server running - connection fails, raises ECUNoResponse."""
        _req = api_types.AbortRequest.convert(DUMMY_ABORT_REQUEST)
        with pytest.raises(ECUNoResponse):
            await OTAClientCall.abort_call(
                ecu_id=self.DUMMY_ECU_ID,
                ecu_ipaddr=self.OTA_CLIENT_SERVICE_IP,
                ecu_port=self.OTA_CLIENT_SERVICE_PORT,
                request=_req,
                timeout=1,
            )

    async def test_abort_call_generic_exception(self, mocker):
        """Non-gRPC exceptions are caught by the generic except branch."""
        _req = api_types.AbortRequest.convert(DUMMY_ABORT_REQUEST)
        mocker.patch(
            "grpc.aio.insecure_channel",
            side_effect=RuntimeError("unexpected"),
        )
        with pytest.raises(
            ECUNoResponse, match="failed to respond to abort request on-time"
        ):
            await OTAClientCall.abort_call(
                ecu_id=self.DUMMY_ECU_ID,
                ecu_ipaddr=self.OTA_CLIENT_SERVICE_IP,
                ecu_port=self.OTA_CLIENT_SERVICE_PORT,
                request=_req,
            )
