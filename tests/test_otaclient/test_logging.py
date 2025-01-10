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

import logging
from dataclasses import dataclass
from queue import Queue

import grpc
import pytest
from pytest_mock import MockerFixture

import otaclient._logging as _logging
from otaclient._logging import LogType, configure_logging
from otaclient.configs._ecu_info import ECUInfo
from otaclient.configs._proxy_info import ProxyInfo
from otaclient.grpc.log_v1 import (
    otaclient_iot_logging_server_v1_pb2 as log_pb2,
)
from otaclient.grpc.log_v1 import (
    otaclient_iot_logging_server_v1_pb2_grpc as log_v1_grpc,
)

logger = logging.getLogger(__name__)

MODULE = _logging.__name__


class TestLogClient:
    OTA_CLIENT_LOGGING_SERVER = "127.0.0.1:8083"

    @dataclass
    class DummyQueueData:
        ecu_id: str
        log_type: log_pb2.LogType
        message: str

    class DummyLogServerService(log_v1_grpc.OtaClientIoTLoggingServiceServicer):
        def __init__(self, client):
            self.client = client

        async def PutLog(self, request: log_pb2.PutLogRequest, context):
            """
            Dummy gRPC method to put a log message to the queue.
            """
            self.client.queue.put(
                TestLogClient.DummyQueueData(
                    ecu_id=request.ecu_id,
                    log_type=request.log_type,
                    message=request.message,
                )
            )
            return log_pb2.PutLogResponse(code=log_pb2.NO_FAILURE)

    @pytest.fixture(autouse=True)
    async def launch_server(self):
        self.queue: Queue[TestLogClient.DummyQueueData] = Queue()

        client = TestLogClient()
        server = grpc.aio.server()
        log_v1_grpc.add_OtaClientIoTLoggingServiceServicer_to_server(
            servicer=TestLogClient.DummyLogServerService(client), server=server
        )
        server.add_insecure_port(TestLogClient.OTA_CLIENT_LOGGING_SERVER)
        try:
            await server.start()
            yield
        finally:
            await server.stop(None)

    @pytest.fixture(autouse=True)
    def mock_ecu_info(self, mocker: MockerFixture):
        self._ecu_info = ECUInfo(ecu_id="otaclient")
        mocker.patch(f"{MODULE}.ecu_info", self._ecu_info)

    @pytest.fixture(autouse=True)
    def mock_proxy_info(self, mocker: MockerFixture):
        self._proxy_info = ProxyInfo(
            logging_server=TestLogClient.OTA_CLIENT_LOGGING_SERVER
        )
        mocker.patch(f"{MODULE}.proxy_info", self._proxy_info)

    @pytest.mark.parametrize(
        "log_message, extra, expected_log_type, expected_message",
        [
            (None, {}, log_pb2.LogType.LOG, "None"),
            (
                "some log message without extra",
                {},
                log_pb2.LogType.LOG,
                "some log message without extra",
            ),
            (
                "some log message",
                {"log_type": LogType.LOG},
                log_pb2.LogType.LOG,
                "some log message",
            ),
            (
                "some metrics message",
                {"log_type": LogType.METRICS},
                log_pb2.LogType.METRICS,
                "some metrics message",
            ),
        ],
    )
    async def test_logging(
        self, log_message, extra, expected_log_type, expected_message
    ):
        configure_logging()
        # send a test log message
        logger.info(log_message, extra=extra)

        _response = self.queue.get()
        assert _response.ecu_id == "otaclient"
        assert _response.log_type == expected_log_type
        assert _response.message == expected_message
