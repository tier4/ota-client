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


from __future__ import annotations

import asyncio

import pytest
import pytest_mock

from otaclient.app.main import create_otaclient_grpc_server
from otaclient.configs import ECUInfo
from otaclient.configs.cfg import cfg as otaclient_cfg
from otaclient_api.v2 import types as api_types
from otaclient_api.v2.api_caller import OTAClientCall
from tests.utils import compare_message

OTACLIENT_APP_MAIN = "otaclient.app.main"


class _MockedOTAClientAPIServicer:
    MY_ECU_ID = "autoware"
    UPDATE_RESP_ECU = api_types.UpdateResponseEcu(
        ecu_id=MY_ECU_ID,
        result=api_types.FailureType.NO_FAILURE,
    )
    UPDATE_RESP = api_types.UpdateResponse(ecu=[UPDATE_RESP_ECU])
    ROLLBACK_RESP_ECU = api_types.RollbackResponseEcu(
        ecu_id=MY_ECU_ID,
        result=api_types.FailureType.NO_FAILURE,
    )
    ROLLBACK_RESP = api_types.RollbackResponse(ecu=[ROLLBACK_RESP_ECU])
    STATUS_RESP_ECU = api_types.StatusResponseEcuV2(
        ecu_id=MY_ECU_ID,
        otaclient_version="mocked_otaclient",
        firmware_version="firmware",
        ota_status=api_types.StatusOta.SUCCESS,
        failure_type=api_types.FailureType.NO_FAILURE,
    )
    STATUS_RESP = api_types.StatusResponse(
        available_ecu_ids=[MY_ECU_ID], ecu_v2=[STATUS_RESP_ECU]
    )

    async def update(self, *arg, **kwargs):
        return self.UPDATE_RESP

    async def rollback(self, *args, **kwargs):
        return self.ROLLBACK_RESP

    async def status(self, *args, **kwargs):
        return self.STATUS_RESP


class TestOTAClientAPIServer:
    MY_ECU_ID = _MockedOTAClientAPIServicer.MY_ECU_ID
    LISTEN_ADDR = "127.0.0.1"
    LISTEN_PORT = otaclient_cfg.OTA_API_SERVER_PORT

    @pytest.fixture(autouse=True)
    def setup_test(self, mocker: pytest_mock.MockerFixture):
        self.otaclient_service_stub = _MockedOTAClientAPIServicer()
        mocker.patch(
            f"{OTACLIENT_APP_MAIN}.OTAClientAPIServicer",
            return_value=self.otaclient_service_stub,
        )

        self.ecu_info_mock = ecu_info_mock = ECUInfo(
            ecu_id=self.otaclient_service_stub.MY_ECU_ID,
            ip_addr=self.LISTEN_ADDR,  # type: ignore
        )
        mocker.patch(f"{OTACLIENT_APP_MAIN}.ecu_info", ecu_info_mock)

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
        update_resp = await OTAClientCall.update_call(
            ecu_id=self.MY_ECU_ID,
            ecu_ipaddr=self.LISTEN_ADDR,
            ecu_port=self.LISTEN_PORT,
            request=api_types.UpdateRequest(),
        )
        compare_message(update_resp, self.otaclient_service_stub.UPDATE_RESP)

        # --- test rollback call --- #
        rollback_resp = await OTAClientCall.rollback_call(
            ecu_id=self.MY_ECU_ID,
            ecu_ipaddr=self.LISTEN_ADDR,
            ecu_port=self.LISTEN_PORT,
            request=api_types.RollbackRequest(),
        )
        compare_message(rollback_resp, self.otaclient_service_stub.ROLLBACK_RESP)

        # --- test status call --- #
        status_resp = await OTAClientCall.status_call(
            ecu_id=self.MY_ECU_ID,
            ecu_ipaddr=self.LISTEN_ADDR,
            ecu_port=self.LISTEN_PORT,
            request=api_types.StatusRequest(),
        )
        compare_message(status_resp, self.otaclient_service_stub.STATUS_RESP)
