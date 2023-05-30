# Copyright 2023 TIER IV, INC. All rights reserved.
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
import pytest
import pytest_mock

from otaclient.app.configs import server_cfg
from otaclient.app.ecu_info import ECUInfo
from otaclient.app.ota_client_service import create_otaclient_grpc_server
from otaclient.app.ota_client_call import OtaClientCall
from otaclient.app.proto import wrapper
from tests.conftest import cfg
from tests.utils import compare_message


class _MockedOTAClientServiceStub:
    MY_ECU_ID = "autoware"
    UPDATE_RESP_ECU = wrapper.UpdateResponseEcu(
        ecu_id=MY_ECU_ID,
        result=wrapper.FailureType.NO_FAILURE,
    )
    UPDATE_RESP = wrapper.UpdateResponse(ecu=[UPDATE_RESP_ECU])
    ROLLBACK_RESP_ECU = wrapper.RollbackResponseEcu(
        ecu_id=MY_ECU_ID,
        result=wrapper.FailureType.NO_FAILURE,
    )
    ROLLBACK_RESP = wrapper.RollbackResponse(ecu=[ROLLBACK_RESP_ECU])
    STATUS_RESP_ECU = wrapper.StatusResponseEcuV2(
        ecu_id=MY_ECU_ID,
        otaclient_version="mocked_otaclient",
        firmware_version="firmware",
        ota_status=wrapper.StatusOta.SUCCESS,
        failure_type=wrapper.FailureType.NO_FAILURE,
    )
    STATUS_RESP = wrapper.StatusResponse(
        available_ecu_ids=[MY_ECU_ID], ecu_v2=[STATUS_RESP_ECU]
    )

    async def update(self, *arg, **kwargs):
        return self.UPDATE_RESP

    async def rollback(self, *args, **kwargs):
        return self.ROLLBACK_RESP

    async def status(self, *args, **kwargs):
        return self.STATUS_RESP


class Test_ota_client_service:
    MY_ECU_ID = _MockedOTAClientServiceStub.MY_ECU_ID
    LISTEN_ADDR = "127.0.0.1"
    LISTEN_PORT = server_cfg.SERVER_PORT

    @pytest.fixture(autouse=True)
    def setup_test(self, mocker: pytest_mock.MockerFixture):
        self.otaclient_service_stub = _MockedOTAClientServiceStub()
        mocker.patch(
            f"{cfg.OTACLIENT_SERVICE_MODULE_PATH}.OTAClientServiceStub",
            return_value=self.otaclient_service_stub,
        )

        ecu_info_mock = mocker.MagicMock(spec=ECUInfo)
        # NOTE: mocked to use 127.0.0.1, and still use server_cfg.SERVER_PORT
        ecu_info_mock.parse_ecu_info.return_value = ECUInfo(
            ecu_id=self.otaclient_service_stub.MY_ECU_ID,
            ip_addr=self.LISTEN_ADDR,
        )
        mocker.patch(f"{cfg.OTACLIENT_SERVICE_MODULE_PATH}.ECUInfo", ecu_info_mock)

    @pytest.fixture(autouse=True)
    async def launch_otaclient_server(self, setup_test):
        server = create_otaclient_grpc_server()
        try:
            await server.start()
            await asyncio.sleep(0.1)  # wait for fully up
            yield
        finally:
            await server.stop(None)

    async def test_otaclient_service(self):
        # --- test update call --- #
        update_resp = await OtaClientCall.update_call(
            ecu_id=self.MY_ECU_ID,
            ecu_ipaddr=self.LISTEN_ADDR,
            ecu_port=self.LISTEN_PORT,
            request=wrapper.UpdateRequest(),
        )
        compare_message(update_resp, self.otaclient_service_stub.UPDATE_RESP)

        # --- test rollback call --- #
        rollback_resp = await OtaClientCall.rollback_call(
            ecu_id=self.MY_ECU_ID,
            ecu_ipaddr=self.LISTEN_ADDR,
            ecu_port=self.LISTEN_PORT,
            request=wrapper.RollbackRequest(),
        )
        compare_message(rollback_resp, self.otaclient_service_stub.ROLLBACK_RESP)

        # --- test status call --- #
        status_resp = await OtaClientCall.status_call(
            ecu_id=self.MY_ECU_ID,
            ecu_ipaddr=self.LISTEN_ADDR,
            ecu_port=self.LISTEN_PORT,
            request=wrapper.StatusRequest(),
        )
        compare_message(status_resp, self.otaclient_service_stub.STATUS_RESP)
