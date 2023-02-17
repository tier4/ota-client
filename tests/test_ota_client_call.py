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
import pytest_asyncio
from otaclient.app.ota_client_call import OtaClientCall

from otaclient.app.proto import v2, v2_grpc, wrapper
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

    async def Status(self, request: v2.StatusRequest, context):
        _res = v2.StatusResponse()
        _res.CopyFrom(self.DUMMY_STATUS)
        return _res


class TestOTAClientCall:
    OTA_CLIENT_SERVICE_PORT = 50051
    OTA_CLIENT_SERVICE_IP = "127.0.0.1"
    DUMMY_ECU_ID = "autoware"

    @pytest_asyncio.fixture(autouse=True)
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

    async def test_update_call(self):
        _req = wrapper.UpdateRequest.convert(
            _DummyOTAClientService.DUMMY_UPDATE_REQUEST
        )
        _response = await OtaClientCall.update_call(
            ecu_id=self.DUMMY_ECU_ID,
            ecu_ipaddr=self.OTA_CLIENT_SERVICE_IP,
            ecu_port=self.OTA_CLIENT_SERVICE_PORT,
            request=_req,
        )
        compare_message(
            _response.export_pb(), _DummyOTAClientService.DUMMY_UPDATE_RESPONSE
        )

    async def test_rollback_call(self):
        _req = wrapper.RollbackRequest.convert(
            _DummyOTAClientService.DUMMY_ROLLBACK_REQUEST
        )
        _response = await OtaClientCall.rollback_call(
            ecu_id=self.DUMMY_ECU_ID,
            ecu_ipaddr=self.OTA_CLIENT_SERVICE_IP,
            ecu_port=self.OTA_CLIENT_SERVICE_PORT,
            request=_req,
        )
        compare_message(
            _response.export_pb(), _DummyOTAClientService.DUMMY_ROLLBACK_RESPONSE
        )

    async def test_status_call(self):
        _response = await OtaClientCall.status_call(
            ecu_id=self.DUMMY_ECU_ID,
            ecu_ipaddr=self.OTA_CLIENT_SERVICE_IP,
            ecu_port=self.OTA_CLIENT_SERVICE_PORT,
        )

        assert _response is not None
        compare_message(_response.export_pb(), _DummyOTAClientService.DUMMY_STATUS)
