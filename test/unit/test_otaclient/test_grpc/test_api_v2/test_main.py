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

import pytest
from pytest_mock import MockerFixture

from otaclient.grpc.api_v2.main import grpc_server_process


class _AsyncServerMock:
    """Stand-in for `grpc.aio.server()` with awaitable start/stop/wait."""

    def __init__(self):
        self.start_called = False
        self.stop_called = False
        self.stop_args = None
        self.add_insecure_port_called = False
        self.add_insecure_port_args = None
        self.add_generic_rpc_handlers_called = False
        self.add_generic_rpc_handlers_args = None
        self.wait_for_termination_called = False

    async def start(self):
        self.start_called = True

    async def stop(self, grace):
        self.stop_called = True
        self.stop_args = grace

    def add_insecure_port(self, address_info):
        self.add_insecure_port_called = True
        self.add_insecure_port_args = address_info

    def add_generic_rpc_handlers(self, handlers):
        self.add_generic_rpc_handlers_called = True
        self.add_generic_rpc_handlers_args = handlers

    async def wait_for_termination(self):
        self.wait_for_termination_called = True


class TestGrpcServerLauncher:
    @pytest.fixture
    def mock_server(self, mocker: MockerFixture) -> _AsyncServerMock:
        server = _AsyncServerMock()

        mocker.patch("otaclient.grpc.api_v2.ecu_status.ECUStatusStorage")
        mocker.patch("otaclient.grpc.api_v2.ecu_tracker.ECUTracker")
        mocker.patch("otaclient.grpc.api_v2.servicer.OTAClientAPIServicer")
        mocker.patch("otaclient_api.v2.api_stub.OtaClientServiceV2")
        mocker.patch(
            "otaclient_pb2.v2.otaclient_v2_pb2_grpc.add_OtaClientServiceServicer_to_server"
        )
        mocker.patch("grpc.aio.server", return_value=server)
        mocker.patch("otaclient._logging.configure_logging")

        async def _noop_sleep(_):
            return None

        mocker.patch("asyncio.sleep", side_effect=_noop_sleep)

        return server

    def test_grpc_server_start(
        self, mock_server: _AsyncServerMock, mocker: MockerFixture
    ):
        def _shm_reader_factory():
            shm_reader = mocker.MagicMock()
            shm_reader.atexit = mocker.MagicMock()
            shm_reader.sync_msg.return_value = mocker.MagicMock()
            return shm_reader

        # Run the coroutine asyncio.run was given on a fresh loop so the
        # patched server methods are actually awaited.
        def _run_and_execute_coroutine(coro):
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

        mocker.patch("asyncio.run", side_effect=_run_and_execute_coroutine)
        mocker.patch("time.sleep")

        grpc_server_process(
            shm_reader_factory=_shm_reader_factory,
            op_queue=mocker.MagicMock(),
            resp_queue=mocker.MagicMock(),
            ecu_status_flags=mocker.MagicMock(),
        )

        assert mock_server.start_called is True
        assert mock_server.stop_called is True
        assert mock_server.stop_args == 1
        assert mock_server.add_insecure_port_called is True
