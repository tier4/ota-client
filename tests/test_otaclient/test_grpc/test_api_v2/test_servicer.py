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
import logging
from concurrent.futures import ThreadPoolExecutor
from ipaddress import IPv4Address

import pytest
from pytest_mock import MockerFixture

from otaclient._types import (
    IPCResEnum,
    IPCResponse,
    UpdateRequestV2,
)
from otaclient.configs._ecu_info import ECUContact, ECUInfo
from otaclient.grpc.api_v2.ecu_status import ECUStatusStorage
from otaclient.grpc.api_v2.servicer import OTAClientAPIServicer
from otaclient_api.v2 import _types as api_types
from otaclient_api.v2.api_caller import ECUNoResponse
from tests.utils import compare_message

logger = logging.getLogger(__name__)

SERVICER_MODULE = "otaclient.grpc.api_v2.servicer"


class TestOTAClientAPIServicer:
    @pytest.fixture(autouse=True)
    async def setup_test(self, mocker: MockerFixture, ecu_info_fixture: ECUInfo):
        # ------ load test ecu_info.yaml ------ #
        self.ecu_info = ecu_info = ecu_info_fixture

        # ------ apply module patches ------ #
        mocker.patch(f"{SERVICER_MODULE}.ecu_info", ecu_info)
        mocker.patch(
            f"{SERVICER_MODULE}.otaclient_cfg.ECU_INFO_LOADED_SUCCESSFULLY", True
        )

        # Get the mock configurations
        self.mock_cfg = mocker.patch(f"{SERVICER_MODULE}.cfg")
        self.mock_cfg.OTA_API_SERVER_PORT = 50051
        self.mock_cfg.WAITING_SUBECU_ACK_REQ_TIMEOUT = 5

        # Setup mocks for queues and executor
        self.op_queue = mocker.MagicMock()
        self.resp_queue = mocker.MagicMock()
        self.executor = mocker.MagicMock(spec=ThreadPoolExecutor)

        # Setup mock for ECUStatusStorage
        self.ecu_status_storage = mocker.MagicMock(spec=ECUStatusStorage)
        self.polling_waiter = mocker.AsyncMock()
        self.ecu_status_storage.get_polling_waiter.return_value = self.polling_waiter
        self.ecu_status_storage.on_ecus_accept_update_request = mocker.AsyncMock()
        self.ecu_status_storage.export = mocker.AsyncMock()

        # Setup mock for CriticalZoneFlag and StopOTAFlag
        self.critical_zone_flag = mocker.MagicMock()
        self.critical_zone_flag.is_set.return_value = False

        self.stop_ota_flag = mocker.MagicMock()
        self.stop_ota_flag.shutdown_requested.return_value = False

        # Create the servicer instance
        self.servicer = OTAClientAPIServicer(
            ecu_status_storage=self.ecu_status_storage,
            op_queue=self.op_queue,
            resp_queue=self.resp_queue,
            critical_zone_flag=self.critical_zone_flag,
            stop_ota_flag=self.stop_ota_flag,
            executor=self.executor,
        )

        # Setup gen_session_id mock
        self.mock_gen_session_id = mocker.patch(f"{SERVICER_MODULE}.gen_session_id")
        self.mock_gen_session_id.return_value = "test-session-id"

        yield

    @pytest.mark.parametrize(
        "local_response_data,expected_ecu_response",
        [
            # Case 0: Successful response
            (
                {
                    "session_id": "test-session-id",
                    "res": IPCResEnum.ACCEPT,
                    "msg": "OK",
                },
                api_types.UpdateResponseEcu(
                    ecu_id="autoware",
                    result=api_types.FailureType.NO_FAILURE,
                ),
            ),
            # Case 1: Rejected response
            (
                {
                    "session_id": "test-session-id",
                    "res": IPCResEnum.REJECT_OTHER,
                    "msg": "Rejected for testing",
                },
                api_types.UpdateResponseEcu(
                    ecu_id="autoware",
                    result=api_types.FailureType.RECOVERABLE,
                ),
            ),
            # Case 2: Exception case - session_id mismatch
            (
                {
                    "session_id": "wrong-session-id",
                    "res": IPCResEnum.ACCEPT,
                    "msg": "OK",
                },
                api_types.UpdateResponseEcu(
                    ecu_id="autoware",
                    result=api_types.FailureType.RECOVERABLE,
                ),
            ),
        ],
    )
    def test_dispatch_local_request(self, local_response_data, expected_ecu_response):
        # Arrange
        request = UpdateRequestV2(
            version="1.0.0",
            url_base="http://example.com",
            cookies_json="{}",
            request_id="test-request-id",
            session_id="test-session-id",
        )

        # Setup response queue to return the specified response
        self.resp_queue.get.return_value = IPCResponse(
            session_id=local_response_data["session_id"],
            res=local_response_data["res"],
            msg=local_response_data["msg"],
        )

        # Act
        if local_response_data["session_id"] != "test-session-id":
            # This will cause an assertion error in _dispatch_local_request
            result = self.servicer._dispatch_local_request(
                request, api_types.UpdateResponseEcu
            )
        else:
            result = self.servicer._dispatch_local_request(
                request, api_types.UpdateResponseEcu
            )

        # Assert
        self.op_queue.put_nowait.assert_called_once_with(request)
        self.resp_queue.get.assert_called_once()
        compare_message(result, expected_ecu_response)

    def test_dispatch_local_request_timeout(self):
        # Arrange
        request = UpdateRequestV2(
            version="1.0.0",
            url_base="http://example.com",
            cookies_json="{}",
            request_id="test-request-id",
            session_id="test-session-id",
        )

        # Setup response queue to simulate timeout
        self.resp_queue.get.side_effect = asyncio.TimeoutError("Timeout")

        # Act
        result = self.servicer._dispatch_local_request(
            request, api_types.UpdateResponseEcu
        )

        # Assert
        expected_response = api_types.UpdateResponseEcu(
            ecu_id="autoware",
            result=api_types.FailureType.UNRECOVERABLE,
        )
        compare_message(result, expected_response)
        self.op_queue.put_nowait.assert_called_once_with(request)
        self.resp_queue.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_ecu_info_not_loaded(self, mocker: MockerFixture):
        # Arrange
        mocker.patch(
            f"{SERVICER_MODULE}.otaclient_cfg.ECU_INFO_LOADED_SUCCESSFULLY", False
        )

        update_request = api_types.UpdateRequest()
        update_request.add_ecu(
            api_types.UpdateRequestEcu(
                ecu_id="autoware",
                version="1.0.0",
                url="http://example.com",
                cookies="{}",
            )
        )

        # Act
        result = await self.servicer.update(update_request)

        # Assert
        expected_response = api_types.UpdateResponse()
        expected_response.add_ecu(
            api_types.UpdateResponseEcu(
                ecu_id="autoware",
                result=api_types.FailureType.UNRECOVERABLE,
            )
        )

        compare_message(result, expected_response)
        self.op_queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_local_ecu_success(self, mocker: MockerFixture):
        # Arrange
        update_request = api_types.UpdateRequest()
        update_request.add_ecu(
            api_types.UpdateRequestEcu(
                ecu_id="autoware",
                version="1.0.0",
                url="http://example.com",
                cookies="{}",
            )
        )

        # Setup response for local update
        self.resp_queue.get.return_value = IPCResponse(
            session_id="test-session-id",
            res=IPCResEnum.ACCEPT,
            msg="OK",
        )

        # Setup executor to run the task and return result
        async def mock_run_in_executor(executor, func, *args):
            # Call the actual function and return its result
            return func(*args)

        loop_mock = mocker.AsyncMock()
        loop_mock.run_in_executor = mock_run_in_executor
        mocker.patch("asyncio.get_running_loop", return_value=loop_mock)

        # Act
        result = await self.servicer.update(update_request)

        # Assert
        expected_response = api_types.UpdateResponse()
        expected_response.add_ecu(
            api_types.UpdateResponseEcu(
                ecu_id="autoware",
                result=api_types.FailureType.NO_FAILURE,
            )
        )

        compare_message(result, expected_response)
        self.op_queue.put_nowait.assert_called_once()
        self.ecu_status_storage.on_ecus_accept_update_request.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_with_sub_ecus(self, mocker: MockerFixture):
        # Create mock sub_ecus without modifying ecu_info.secondaries
        mock_sub_ecus = [
            ECUContact(ecu_id="ecu1", ip_addr=IPv4Address("192.168.1.2"), port=50051)
        ]

        # Patch the property on the servicer instance instead of modifying ecu_info
        mocker.patch.object(self.servicer, "sub_ecus", mock_sub_ecus)

        # Arrange
        update_request = api_types.UpdateRequest()
        # Add main ECU
        update_request.add_ecu(
            api_types.UpdateRequestEcu(
                ecu_id="autoware",
                version="1.0.0",
                url="http://example.com",
                cookies="{}",
            )
        )
        # Add sub ECU
        update_request.add_ecu(
            api_types.UpdateRequestEcu(
                ecu_id="ecu1",
                version="1.0.0",
                url="http://example.com",
                cookies="{}",
            )
        )

        # Configure mock sub ECUs
        mocker.patch.object(self.servicer, "sub_ecus", mock_sub_ecus)

        # Setup response for local update
        self.resp_queue.get.return_value = IPCResponse(
            session_id="test-session-id",
            res=IPCResEnum.ACCEPT,
            msg="OK",
        )

        # Setup sub ECU response
        sub_ecu_response = api_types.UpdateResponse()
        sub_ecu_response.add_ecu(
            api_types.UpdateResponseEcu(
                ecu_id="ecu1",
                result=api_types.FailureType.NO_FAILURE,
            )
        )
        sub_ecu_response.ecus_acked_update = {"ecu1"}

        # Mock OTAClientCall.update_call
        mock_update_call = mocker.patch(
            "otaclient_api.v2.api_caller.OTAClientCall.update_call",
            new_callable=mocker.AsyncMock,
        )
        mock_update_call.return_value = sub_ecu_response

        # Setup executor for local handler
        async def mock_run_in_executor(executor, func, *args):
            return func(*args)

        loop_mock = mocker.AsyncMock()
        loop_mock.run_in_executor = mock_run_in_executor
        mocker.patch("asyncio.get_running_loop", return_value=loop_mock)

        # Act
        result = await self.servicer.update(update_request)

        # Assert
        expected_response = api_types.UpdateResponse()
        expected_response.add_ecu(
            api_types.UpdateResponseEcu(
                ecu_id="ecu1",
                result=api_types.FailureType.NO_FAILURE,
            )
        )
        expected_response.add_ecu(
            api_types.UpdateResponseEcu(
                ecu_id="autoware",
                result=api_types.FailureType.NO_FAILURE,
            )
        )

        assert len(list(result.iter_ecu())) == 2
        assert any(resp.ecu_id == "autoware" for resp in result.iter_ecu())
        assert any(resp.ecu_id == "ecu1" for resp in result.iter_ecu())

        mock_update_call.assert_called_once()
        self.ecu_status_storage.on_ecus_accept_update_request.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_subecu_no_response(self, mocker: MockerFixture):
        # Arrange
        update_request = api_types.UpdateRequest()
        # Add sub ECU only
        update_request.add_ecu(
            api_types.UpdateRequestEcu(
                ecu_id="ecu1",
                version="1.0.0",
                url="http://example.com",
                cookies="{}",
            )
        )

        # Configure mock sub ECUs - patch the servicer property, not the ECUInfo model
        sub_ecu = ECUContact(
            ecu_id="ecu1", ip_addr=IPv4Address("192.168.1.2"), port=50051
        )
        mocker.patch.object(self.servicer, "sub_ecus", [sub_ecu])

        # Mock OTAClientCall.update_call to raise ECUNoResponse
        mock_update_call = mocker.patch(
            "otaclient_api.v2.api_caller.OTAClientCall.update_call",
            new_callable=mocker.AsyncMock,
        )
        mock_update_call.side_effect = ECUNoResponse("Sub ECU did not respond")

        # Act
        result = await self.servicer.update(update_request)

        # Assert
        expected_response = api_types.UpdateResponse()
        expected_response.add_ecu(
            api_types.UpdateResponseEcu(
                ecu_id="ecu1",
                result=api_types.FailureType.RECOVERABLE,
            )
        )

        compare_message(result, expected_response)
        mock_update_call.assert_called_once()
        self.ecu_status_storage.on_ecus_accept_update_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_rollback_local_ecu(self, mocker: MockerFixture):
        """Ensure that rollback is not triggerred."""
        # Arrange
        rollback_request = api_types.RollbackRequest()
        rollback_request.add_ecu(
            api_types.RollbackRequestEcu(
                ecu_id="autoware",
            )
        )

        # Setup response for local rollback
        self.resp_queue.get.return_value = IPCResponse(
            session_id="test-session-id",
            res=IPCResEnum.ACCEPT,
            msg="OK",
        )

        # Setup executor to run the task and return result
        async def mock_run_in_executor(executor, func, *args):
            return func(*args)

        loop_mock = mocker.AsyncMock()
        loop_mock.run_in_executor = mock_run_in_executor
        mocker.patch("asyncio.get_running_loop", return_value=loop_mock)

        # Act
        result = await self.servicer.rollback(rollback_request)

        # Assert
        expected_response = api_types.RollbackResponse()
        expected_response.add_ecu(
            api_types.RollbackResponseEcu(
                ecu_id="autoware",
                result=api_types.FailureType.RECOVERABLE,
                message="rollback API support is removed",
            )
        )

        compare_message(result, expected_response)
        self.op_queue.put_nowait.assert_not_called()
        self.ecu_status_storage.on_ecus_accept_update_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_client_update_local_ecu(self, mocker: MockerFixture):
        # Arrange
        client_update_request = api_types.ClientUpdateRequest()
        client_update_request.add_ecu(
            api_types.ClientUpdateRequestEcu(
                ecu_id="autoware",
                version="1.0.0",
                url="http://example.com",
                cookies="{}",
            )
        )

        # Setup response for local update
        self.resp_queue.get.return_value = IPCResponse(
            session_id="test-session-id",
            res=IPCResEnum.ACCEPT,
            msg="OK",
        )

        # Setup executor to run the task and return result
        async def mock_run_in_executor(executor, func, *args):
            return func(*args)

        loop_mock = mocker.AsyncMock()
        loop_mock.run_in_executor = mock_run_in_executor
        mocker.patch("asyncio.get_running_loop", return_value=loop_mock)

        # Act
        result = await self.servicer.client_update(client_update_request)

        # Assert
        expected_response = api_types.ClientUpdateResponse()
        expected_response.add_ecu(
            api_types.ClientUpdateResponseEcu(
                ecu_id="autoware",
                result=api_types.FailureType.NO_FAILURE,
            )
        )

        compare_message(result, expected_response)
        self.op_queue.put_nowait.assert_called_once()
        self.ecu_status_storage.on_ecus_accept_update_request.assert_called_once()

    @pytest.mark.asyncio
    async def test_status(self):
        # Arrange
        expected_response = api_types.StatusResponse()
        self.ecu_status_storage.export.return_value = expected_response

        # Act
        result = await self.servicer.status()

        # Assert
        self.ecu_status_storage.export.assert_called_once()
        assert result == expected_response
