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
"""Integration tests for the otaclient logging subsystem.

The scope here is the full ``configure_logging()`` -> ``_LogTeeHandler``
-> ``TransmitterGrpc`` -> live gRPC server flow.  A dummy
``OTAClientIoTLoggingService`` runs in-process on a loopback port and
captures the messages that the logging handler streams to it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from queue import Queue
from urllib.parse import urlparse

import grpc
import pytest
import pytest_asyncio
from otaclient_iot_logging_server_pb2.v1 import (
    otaclient_iot_logging_server_v1_pb2 as log_pb2,
)
from otaclient_iot_logging_server_pb2.v1 import (
    otaclient_iot_logging_server_v1_pb2_grpc as log_v1_grpc,
)
from pydantic import AnyHttpUrl
from pytest_mock import MockerFixture

import otaclient._logging as _logging
from otaclient._logging import LogType, _LogTeeHandler, configure_logging
from otaclient.configs._cfg_configurable import _OTAClientSettings
from otaclient.configs._ecu_info import ECUInfo
from otaclient.configs._proxy_info import ProxyInfo

logger = logging.getLogger(__name__)

MODULE = _logging.__name__


@dataclass
class DummyQueueData:
    ecu_id: str
    log_type: log_pb2.LogType
    message: str


class DummyLogServerService(log_v1_grpc.OTAClientIoTLoggingServiceServicer):
    def __init__(self, test_queue: Queue[DummyQueueData], data_ready: asyncio.Event):
        self._test_queue = test_queue
        self._data_ready = data_ready

    async def Check(self, request: log_pb2.HealthCheckRequest, context):
        return log_pb2.HealthCheckResponse(
            status=log_pb2.HealthCheckResponse.ServingStatus.SERVING
        )

    async def PutLog(self, request: log_pb2.PutLogRequest, context):
        self._test_queue.put_nowait(
            DummyQueueData(
                ecu_id=request.ecu_id,
                log_type=request.log_type,
                message=request.message,
            )
        )
        self._data_ready.set()
        return log_pb2.PutLogResponse(code=log_pb2.NO_FAILURE)


class TestLogClient:
    OTA_CLIENT_LOGGING_SERVER = "http://127.0.0.1:8083"
    OTA_CLIENT_LOGGING_SERVER_GRPC = "http://192.168.0.1:8084"
    GRPC_BIND_URL = "http://127.0.0.1:8084"
    ECU_ID = "testclient"

    @pytest.fixture(autouse=True)
    async def initialize_queue(self):
        self.test_queue: Queue[DummyQueueData] = Queue()
        self.data_ready = asyncio.Event()
        self.data_ready.clear()

    @pytest.fixture(autouse=True)
    def mock_cfg(self, mocker: MockerFixture):
        self._cfg = _OTAClientSettings(
            LOG_LEVEL_TABLE={__name__: "INFO"},
        )
        mocker.patch(f"{MODULE}.cfg", self._cfg)

    @pytest.fixture(autouse=True)
    def mock_ecu_info(self, mocker: MockerFixture):
        self._ecu_info = ECUInfo(ecu_id=TestLogClient.ECU_ID)
        mocker.patch(f"{MODULE}.ecu_info", self._ecu_info)

    @pytest.fixture(autouse=True)
    def mock_proxy_info(self, mocker: MockerFixture):
        self._proxy_info = ProxyInfo(
            logging_server=AnyHttpUrl(TestLogClient.OTA_CLIENT_LOGGING_SERVER),
            logging_server_grpc=AnyHttpUrl(
                TestLogClient.OTA_CLIENT_LOGGING_SERVER_GRPC
            ),
        )
        mocker.patch(f"{MODULE}.proxy_info", self._proxy_info)

    @pytest_asyncio.fixture
    async def launch_grpc_server(self):
        server = grpc.aio.server()
        log_v1_grpc.add_OTAClientIoTLoggingServiceServicer_to_server(
            servicer=DummyLogServerService(self.test_queue, self.data_ready),
            server=server,
        )
        parsed_url = urlparse(TestLogClient.GRPC_BIND_URL)
        server.add_insecure_port(parsed_url.netloc)
        try:
            await server.start()
            yield
        finally:
            await server.stop(None)
            await server.wait_for_termination()

    @pytest.fixture
    def restore_logging(self):
        # configure_logging() mutates the root logger; snapshot and restore so
        # the global state is the same after each test.
        original_handlers = logging.root.handlers[:]
        original_level = logging.root.level
        original_formatters = [handler.formatter for handler in logging.root.handlers]

        yield

        logging.root.handlers = original_handlers
        logging.root.level = original_level
        for handler, formatter in zip(logging.root.handlers, original_formatters):
            handler.setFormatter(formatter)

    @pytest.mark.parametrize(
        "log_message, extra, expected_log_type",
        [
            pytest.param(
                "some log message without extra",
                {},
                log_pb2.LogType.LOG,
                id="default-log-type",
            ),
            pytest.param(
                "some log message",
                {"log_type": LogType.LOG},
                log_pb2.LogType.LOG,
                id="explicit-log-type",
            ),
            pytest.param(
                "some metrics message",
                {"log_type": LogType.METRICS},
                log_pb2.LogType.METRICS,
                id="metrics-log-type",
            ),
        ],
    )
    async def test_grpc_logging(
        self,
        launch_grpc_server,
        restore_logging,
        log_message,
        extra,
        expected_log_type,
        mocker: MockerFixture,
    ):
        self.data_ready.clear()

        mocker.patch.object(
            _LogTeeHandler, "_wait_for_log_server_up", return_value=None
        )
        configure_logging()

        # Give some time for the logging handler to be fully set up
        await asyncio.sleep(0.1)

        # send a test log message
        logger.error(log_message, extra=extra)
        # wait for the log message to be received
        try:
            await asyncio.wait_for(self.data_ready.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pytest.fail("Timed out waiting for log message")

        try:
            _response = self.test_queue.get_nowait()
        except Exception as e:
            pytest.fail(f"Failed to get a log message from the queue: {e}")

        assert _response.ecu_id == TestLogClient.ECU_ID
        assert _response.log_type == expected_log_type

        if _response.log_type == log_pb2.LogType.LOG:
            # The handler formats LOG records as JSON; extract `message` to
            # compare against the original input.
            try:
                log_data = json.loads(_response.message)
                extracted_message = log_data["message"]
            except (json.JSONDecodeError, KeyError) as e:
                pytest.fail(f"Failed to parse JSON log message: {e}")
            assert extracted_message == log_message
        elif _response.log_type == log_pb2.LogType.METRICS:
            # METRICS records pass through unchanged.
            assert _response.message == log_message
        else:
            pytest.fail(f"Unexpected log type: {_response.log_type}")

    def test_configure_logging(self, restore_logging, mocker: MockerFixture):
        mock_start_upload_thread = mocker.patch.object(
            _logging._LogTeeHandler, "start_upload_thread"
        )

        configure_logging()

        mock_start_upload_thread.assert_called_once_with(
            logging_upload_endpoint=self._proxy_info.logging_server,
            logging_upload_grpc_endpoint=AnyHttpUrl(TestLogClient.GRPC_BIND_URL),
            ecu_id=self._ecu_info.ecu_id,
        )
