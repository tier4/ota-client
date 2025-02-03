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
"""Configure the logging for otaclient."""


from __future__ import annotations

import atexit
import contextlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from queue import Queue
from threading import Event, Thread
from urllib.parse import urljoin, urlparse

import grpc
import requests
from pydantic import AnyHttpUrl

from otaclient.configs.cfg import cfg, ecu_info, proxy_info
from otaclient.grpc.log_v1 import (
    otaclient_iot_logging_server_v1_pb2 as log_pb2,
)
from otaclient.grpc.log_v1 import (
    otaclient_iot_logging_server_v1_pb2_grpc as log_v1_grpc,
)


class LogType(Enum):
    LOG = "log"
    METRICS = "metrics"


@dataclass
class QueueData:
    """Queue data format for logging."""

    log_type: LogType
    message: str


class Transmitter(ABC):
    def __init__(self, logging_upload_endpoint, ecu_id: str):
        self.ecu_id = ecu_id

    @abstractmethod
    def send(self, log_type: LogType, message: str, timeout: int) -> None:
        pass


class TransmitterGrpc(Transmitter):
    def __init__(self, logging_upload_endpoint: AnyHttpUrl, ecu_id: str):
        super().__init__(logging_upload_endpoint, ecu_id)

        parsed_url = urlparse(str(logging_upload_endpoint))
        log_upload_endpoint = parsed_url.netloc
        channel = grpc.insecure_channel(log_upload_endpoint)
        self._stub = log_v1_grpc.OtaClientIoTLoggingServiceStub(channel)

    def send(self, log_type: LogType, message: str, timeout: int) -> None:
        pb2_log_type = (
            log_pb2.LogType.METRICS
            if log_type == LogType.METRICS
            else log_pb2.LogType.LOG
        )
        log_entry = log_pb2.PutLogRequest(
            ecu_id=self.ecu_id, log_type=pb2_log_type, message=message
        )
        self._stub.PutLog(log_entry, timeout=timeout)

    def check(self, timeout: int) -> None:
        # health check
        log_entry = log_pb2.HealthCheckRequest(service="")
        self._stub.Check(log_entry)


class TransmitterHttp(Transmitter):
    def __init__(self, logging_upload_endpoint, ecu_id: str):
        super().__init__(logging_upload_endpoint, ecu_id)

        logging_upload_endpoint = f"{str(logging_upload_endpoint).strip('/')}/"
        self.log_upload_endpoint = urljoin(logging_upload_endpoint, ecu_id)
        self._session = requests.Session()

    def send(self, log_type: LogType, message: str, timeout: int) -> None:
        # support only LogType.LOG
        with contextlib.suppress(Exception):
            self._session.post(self.log_upload_endpoint, data=message, timeout=timeout)


class TransmitterFactory:
    @staticmethod
    def create(logging_upload_endpoint: AnyHttpUrl, ecu_id: str):
        try:
            _transmitter_grpc = TransmitterGrpc(logging_upload_endpoint, ecu_id)
            _transmitter_grpc.check(timeout=3)
            return _transmitter_grpc
        except Exception:
            return TransmitterHttp(logging_upload_endpoint, ecu_id)


class _LogTeeHandler(logging.Handler):
    """Implementation of teeing local logs to a remote otaclient-iot-logger server."""

    def __init__(self, max_backlog: int = 2048) -> None:
        super().__init__()
        self._queue: Queue[QueueData | None] = Queue(maxsize=max_backlog)

    def emit(self, record: logging.LogRecord) -> None:
        with contextlib.suppress(Exception):
            _log_type = getattr(record, "log_type", LogType.LOG)  # default to LOG
            # if a message is log message, format the message with the formatter
            # otherwise(metric message), use the raw message
            _message = (
                self.format(record) if _log_type == LogType.LOG else record.getMessage()
            )
            self._queue.put_nowait(QueueData(_log_type, _message))

    def start_upload_thread(
        self, logging_upload_endpoint: AnyHttpUrl, ecu_id: str
    ) -> None:
        log_queue = self._queue
        stop_logging_upload = Event()

        def _thread_main():
            _transmitter = TransmitterFactory.create(logging_upload_endpoint, ecu_id)

            while not stop_logging_upload.is_set():
                entry = log_queue.get()
                if entry is None:
                    return  # stop signal
                if not entry:
                    continue  # skip uploading empty log line

                _transmitter.send(entry.log_type, entry.message, timeout=3)

        log_upload_thread = Thread(target=_thread_main, daemon=True)
        log_upload_thread.start()

        def _thread_exit():
            stop_logging_upload.set()
            log_queue.put_nowait(None)
            log_upload_thread.join(6)

        atexit.register(_thread_exit)


def configure_logging() -> None:
    """Configure logging with http/gRPC handler."""
    # ------ suppress logging from non-first-party modules ------ #
    # NOTE: force to reload the basicConfig, this is for overriding setting
    #       when launching subprocess.
    # NOTE: for the root logger, set to CRITICAL to filter away logs from other
    #       external modules unless reached CRITICAL level.
    logging.basicConfig(level=logging.CRITICAL, format=cfg.LOG_FORMAT, force=True)

    # ------ configure each sub loggers and attach ota logging handler ------ #
    log_upload_handler = None
    if logging_upload_endpoint := proxy_info.logging_server:
        log_upload_handler = _LogTeeHandler()
        fmt = logging.Formatter(fmt=cfg.LOG_FORMAT)
        log_upload_handler.setFormatter(fmt)

        # star the logging thread
        log_upload_handler.start_upload_thread(logging_upload_endpoint, ecu_info.ecu_id)

    for logger_name, loglevel in cfg.LOG_LEVEL_TABLE.items():
        _logger = logging.getLogger(logger_name)
        _logger.setLevel(loglevel)
        if log_upload_handler:
            _logger.addHandler(log_upload_handler)
