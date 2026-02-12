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
    AbortRequestV2,
    AbortState,
    IPCResEnum,
    IPCResponse,
    OTAStatus,
    UpdateRequestV2,
)
from otaclient.configs._ecu_info import ECUContact, ECUInfo
from otaclient.grpc.api_v2.ecu_status import ECUStatusStorage
from otaclient.grpc.api_v2.servicer import OTAClientAPIServicer
from otaclient_api.v2 import _types as api_types
from otaclient_api.v2.api_caller import ECUAbortNotSupported, ECUNoResponse
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
        self.mock_cfg.OTA_STATUS_FNAME = "status"

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

        # Setup mock for OTAAbortState
        self.abort_ota_state = mocker.MagicMock()
        self.abort_ota_state.try_set_requested.return_value = True
        self.abort_ota_state.state = mocker.PropertyMock(return_value=None)

        # Setup mock for shared memory reader
        self.shm_reader = mocker.MagicMock()
        self.shm_reader.sync_msg.return_value = mocker.MagicMock(
            ota_status=OTAStatus.UPDATING,
        )

        # Create the servicer instance
        self.servicer = OTAClientAPIServicer(
            ecu_status_storage=self.ecu_status_storage,
            op_queue=self.op_queue,
            resp_queue=self.resp_queue,
            abort_ota_state=self.abort_ota_state,
            shm_reader=self.shm_reader,
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

    # ==================== Abort Tests ====================

    def test_handle_abort_request_success(self):
        """Test abort request succeeds via atomic CAS: NONE → REQUESTED."""
        self.abort_ota_state.try_set_requested.return_value = True

        request = AbortRequestV2(request_id="test-req", session_id="test-session")
        result = self.servicer._handle_abort_request(request)

        assert result.ecu_id == "autoware"
        assert result.result == api_types.AbortFailureType.ABORT_NO_FAILURE
        self.abort_ota_state.try_set_requested.assert_called_once()

    def test_handle_abort_request_already_in_progress(self):
        """Test abort request when abort is already REQUESTED or ABORTING."""
        self.abort_ota_state.try_set_requested.return_value = False
        self.abort_ota_state.state = AbortState.REQUESTED

        request = AbortRequestV2(request_id="test-req", session_id="test-session")
        result = self.servicer._handle_abort_request(request)

        assert result.ecu_id == "autoware"
        assert result.result == api_types.AbortFailureType.ABORT_NO_FAILURE
        assert "already in progress" in result.message

    def test_handle_abort_request_rejected_final_phase(self):
        """Test abort request rejected when in final update phase."""
        self.abort_ota_state.try_set_requested.return_value = False
        self.abort_ota_state.state = AbortState.FINAL_PHASE

        request = AbortRequestV2(request_id="test-req", session_id="test-session")
        result = self.servicer._handle_abort_request(request)

        assert result.ecu_id == "autoware"
        assert result.result == api_types.AbortFailureType.ABORT_FAILURE
        assert "final phase" in result.message

    def test_handle_abort_request_rejected_already_aborted(self):
        """Test abort request rejected when already in ABORTED state."""
        self.abort_ota_state.try_set_requested.return_value = False
        self.abort_ota_state.state = AbortState.ABORTED

        request = AbortRequestV2(request_id="test-req", session_id="test-session")
        result = self.servicer._handle_abort_request(request)

        assert result.ecu_id == "autoware"
        assert result.result == api_types.AbortFailureType.ABORT_FAILURE

    @pytest.mark.parametrize(
        "ota_status",
        [
            OTAStatus.INITIALIZED,
            OTAStatus.SUCCESS,
            OTAStatus.FAILURE,
            OTAStatus.ABORTED,
            OTAStatus.ROLLBACK_FAILURE,
        ],
    )
    def test_handle_abort_request_rejected_no_active_update(
        self, mocker: MockerFixture, ota_status: OTAStatus
    ):
        """Test abort request rejected when no active OTA update is in progress."""
        self.shm_reader.sync_msg.return_value = mocker.MagicMock(
            ota_status=ota_status,
        )

        request = AbortRequestV2(request_id="test-req", session_id="test-session")
        result = self.servicer._handle_abort_request(request)

        assert result.ecu_id == "autoware"
        assert result.result == api_types.AbortFailureType.ABORT_FAILURE
        assert "no active OTA update" in result.message

    def test_handle_abort_request_rejected_shm_reader_returns_none(
        self, mocker: MockerFixture
    ):
        """Test abort request rejected when shm_reader returns None."""
        self.shm_reader.sync_msg.return_value = None

        request = AbortRequestV2(request_id="test-req", session_id="test-session")
        result = self.servicer._handle_abort_request(request)

        assert result.ecu_id == "autoware"
        assert result.result == api_types.AbortFailureType.ABORT_FAILURE
        assert "no active OTA update" in result.message

    def test_handle_abort_request_invalid_request(self):
        """Test abort request with invalid request type."""
        result = self.servicer._handle_abort_request("invalid_request")

        assert result.ecu_id == ""
        assert result.result == api_types.AbortFailureType.ABORT_FAILURE
        assert "invalid abort request" in result.message

    @pytest.mark.asyncio
    async def test_abort_local_ecu_only(self, mocker: MockerFixture):
        """Test abort with local ECU only (no sub-ECUs)."""
        mocker.patch.object(self.servicer, "sub_ecus", [])
        self.abort_ota_state.try_set_requested.return_value = True

        abort_request = api_types.AbortRequest()
        result = await self.servicer.abort(abort_request)

        ecu_responses = list(result.iter_ecu())
        assert len(ecu_responses) == 1
        assert ecu_responses[0].ecu_id == "autoware"
        assert ecu_responses[0].result == api_types.AbortFailureType.ABORT_NO_FAILURE

    @pytest.mark.asyncio
    async def test_abort_with_sub_ecus(self, mocker: MockerFixture):
        """Test abort with sub-ECUs."""
        mock_sub_ecus = [
            ECUContact(ecu_id="ecu1", ip_addr=IPv4Address("192.168.1.2"), port=50051)
        ]
        mocker.patch.object(self.servicer, "sub_ecus", mock_sub_ecus)
        self.abort_ota_state.try_set_requested.return_value = True

        sub_ecu_response = api_types.AbortResponse()
        sub_ecu_response.add_ecu(
            api_types.AbortResponseEcu(
                ecu_id="ecu1",
                result=api_types.AbortFailureType.ABORT_NO_FAILURE,
            )
        )

        mock_abort_call = mocker.patch(
            "otaclient_api.v2.api_caller.OTAClientCall.abort_call",
            new_callable=mocker.AsyncMock,
        )
        mock_abort_call.return_value = sub_ecu_response

        abort_request = api_types.AbortRequest()
        result = await self.servicer.abort(abort_request)

        ecu_responses = list(result.iter_ecu())
        assert len(ecu_responses) == 2
        assert any(resp.ecu_id == "autoware" for resp in ecu_responses)
        assert any(resp.ecu_id == "ecu1" for resp in ecu_responses)
        mock_abort_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_abort_subecu_no_response(self, mocker: MockerFixture):
        """Test abort when sub-ECU doesn't respond."""
        sub_ecu = ECUContact(
            ecu_id="ecu1", ip_addr=IPv4Address("192.168.1.2"), port=50051
        )
        mocker.patch.object(self.servicer, "sub_ecus", [sub_ecu])
        self.abort_ota_state.try_set_requested.return_value = True

        mock_abort_call = mocker.patch(
            "otaclient_api.v2.api_caller.OTAClientCall.abort_call",
            new_callable=mocker.AsyncMock,
        )
        mock_abort_call.side_effect = ECUNoResponse("Sub ECU did not respond")

        abort_request = api_types.AbortRequest()
        result = await self.servicer.abort(abort_request)

        ecu_responses = list(result.iter_ecu())
        assert len(ecu_responses) == 2

        sub_ecu_resp = next(r for r in ecu_responses if r.ecu_id == "ecu1")
        assert sub_ecu_resp.result == api_types.AbortFailureType.ABORT_FAILURE
        assert sub_ecu_resp.message

        local_resp = next(r for r in ecu_responses if r.ecu_id == "autoware")
        assert local_resp.result == api_types.AbortFailureType.ABORT_NO_FAILURE

    @pytest.mark.asyncio
    async def test_abort_subecu_not_supported(self, mocker: MockerFixture):
        """Test abort when sub-ECU doesn't support the abort endpoint."""
        sub_ecu = ECUContact(
            ecu_id="ecu1", ip_addr=IPv4Address("192.168.1.2"), port=50051
        )
        mocker.patch.object(self.servicer, "sub_ecus", [sub_ecu])
        self.abort_ota_state.try_set_requested.return_value = True

        mock_abort_call = mocker.patch(
            "otaclient_api.v2.api_caller.OTAClientCall.abort_call",
            new_callable=mocker.AsyncMock,
        )
        mock_abort_call.side_effect = ECUAbortNotSupported(
            "ecu_id='ecu1' does not support the abort endpoint"
        )

        abort_request = api_types.AbortRequest()
        result = await self.servicer.abort(abort_request)

        ecu_responses = list(result.iter_ecu())
        assert len(ecu_responses) == 2

        sub_ecu_resp = next(r for r in ecu_responses if r.ecu_id == "ecu1")
        assert sub_ecu_resp.result == api_types.AbortFailureType.ABORT_FAILURE
        assert "UNIMPLEMENTED" in sub_ecu_resp.message

        local_resp = next(r for r in ecu_responses if r.ecu_id == "autoware")
        assert local_resp.result == api_types.AbortFailureType.ABORT_NO_FAILURE

    @pytest.mark.asyncio
    async def test_abort_rejected_in_final_phase(self, mocker: MockerFixture):
        """Test that abort is rejected when in final update phase."""
        mocker.patch.object(self.servicer, "sub_ecus", [])
        self.abort_ota_state.try_set_requested.return_value = False
        self.abort_ota_state.state = AbortState.FINAL_PHASE

        abort_request = api_types.AbortRequest()
        result = await self.servicer.abort(abort_request)

        ecu_responses = list(result.iter_ecu())
        assert len(ecu_responses) == 1
        assert ecu_responses[0].ecu_id == "autoware"
        assert ecu_responses[0].result == api_types.AbortFailureType.ABORT_FAILURE
