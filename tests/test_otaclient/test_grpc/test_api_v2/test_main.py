import asyncio
import logging
from unittest.mock import MagicMock, patch

import pytest

from otaclient.grpc.api_v2.main import grpc_server_process

logger = logging.getLogger(__name__)


class TestGrpcServerLauncher:
    @pytest.fixture
    def setup_mocks(self):
        # Create a proper async server mock with methods that return awaitable futures
        class AsyncServerMock:
            def __init__(self):
                self.start_called = False
                self.stop_called = False
                self.stop_args = None
                self.add_insecure_port_called = False
                self.add_insecure_port_args = None
                self.add_generic_rpc_handlers_called = False
                self.add_generic_rpc_handlers_args = None

            async def start(self):
                self.start_called = True
                return None

            async def stop(self, grace):
                self.stop_called = True
                self.stop_args = grace
                return None

            def add_insecure_port(self, address_info):
                self.add_insecure_port_called = True
                self.add_insecure_port_args = address_info
                return None

            def add_generic_rpc_handlers(self, handlers):
                self.add_generic_rpc_handlers_called = True
                self.add_generic_rpc_handlers_args = handlers
                return None

        mock_server = AsyncServerMock()

        with patch(
            "otaclient.grpc.api_v2.ecu_status.ECUStatusStorage"
        ) as mock_ecu_status_storage, patch(
            "otaclient.grpc.api_v2.ecu_tracker.ECUTracker"
        ) as mock_ecu_tracker, patch(
            "otaclient.grpc.api_v2.servicer.OTAClientAPIServicer"
        ) as mock_api_servicer, patch(
            "otaclient_api.v2.api_stub.OtaClientServiceV2"
        ) as mock_ota_client_service_v2, patch(
            "otaclient_api.v2.otaclient_v2_pb2_grpc.add_OtaClientServiceServicer_to_server"
        ) as mock_add_servicer, patch(
            "grpc.aio.server", return_value=mock_server
        ) as mock_grpc_server, patch(
            "asyncio.sleep"
        ) as mock_sleep:

            # Make asyncio.sleep return immediately to speed up tests
            async def mock_sleep_impl(_):
                return None

            mock_sleep.side_effect = mock_sleep_impl

            yield {
                "mock_ecu_status_storage": mock_ecu_status_storage,
                "mock_ecu_tracker": mock_ecu_tracker,
                "mock_api_servicer": mock_api_servicer,
                "mock_ota_client_service_v2": mock_ota_client_service_v2,
                "mock_add_servicer": mock_add_servicer,
                "mock_grpc_server": mock_grpc_server,
                "mock_server": mock_server,
                "mock_sleep": mock_sleep,
            }

    def test_grpc_server_start(self, setup_mocks):
        mocks = setup_mocks
        mock_server = mocks["mock_server"]
        stop_server_event = MagicMock()
        stop_server_event.is_set.side_effect = [False, True]

        def mock_shm_reader_factory():
            mock_shm_reader = MagicMock()
            mock_shm_reader.atexit = MagicMock()
            return mock_shm_reader

        # Run the server process function with patched asyncio.run
        with patch("asyncio.run") as mock_run:

            def run_and_execute_coroutine(coro):
                # Actually run the coroutine function
                loop = asyncio.new_event_loop()
                try:
                    return loop.run_until_complete(coro)
                finally:
                    loop.close()

            mock_run.side_effect = run_and_execute_coroutine

            grpc_server_process(
                shm_reader_factory=mock_shm_reader_factory,
                op_queue=MagicMock(),
                resp_queue=MagicMock(),
                ecu_status_flags=MagicMock(),
                stop_server_event=stop_server_event,
            )

        # Verify server methods were called
        assert mock_server.start_called is True
        assert mock_server.stop_called is True
        assert mock_server.stop_args == 1
        assert mock_server.add_insecure_port_called is True

        # Verify the stop event was checked
        assert stop_server_event.is_set.call_count == 2

    def test_grpc_stop_server_event(self, setup_mocks):
        mocks = setup_mocks
        mock_server = mocks["mock_server"]
        stop_server_event = MagicMock()
        # Configure the stop event to return False twice then True
        stop_server_event.is_set.side_effect = [False, False, True]

        def mock_shm_reader_factory():
            mock_shm_reader = MagicMock()
            mock_shm_reader.atexit = MagicMock()
            return mock_shm_reader

        # Run the server process function with patched asyncio.run
        with patch("asyncio.run") as mock_run:

            def run_and_execute_coroutine(coro):
                # Actually run the coroutine function
                loop = asyncio.new_event_loop()
                try:
                    return loop.run_until_complete(coro)
                finally:
                    loop.close()

            mock_run.side_effect = run_and_execute_coroutine

            grpc_server_process(
                shm_reader_factory=mock_shm_reader_factory,
                op_queue=MagicMock(),
                resp_queue=MagicMock(),
                ecu_status_flags=MagicMock(),
                stop_server_event=stop_server_event,
            )

        # Check that is_set was called the expected number of times
        assert stop_server_event.is_set.call_count == 3
        # Check that the server was started and stopped
        assert mock_server.start_called is True
        assert mock_server.stop_called is True
