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
from queue import Queue
from threading import Event, Thread
from urllib.parse import urljoin

import requests

from otaclient.app.configs import config as cfg
from otaclient.configs.cfg import ecu_info, proxy_info


class _LogTeeHandler(logging.Handler):
    """Implementation of teeing local logs to a remote otaclient-iot-logger server."""

    def __init__(self, max_backlog: int = 2048) -> None:
        super().__init__()
        self._queue: Queue[str | None] = Queue(maxsize=max_backlog)

    def emit(self, record: logging.LogRecord) -> None:
        with contextlib.suppress(Exception):
            self._queue.put_nowait(self.format(record))

    def start_upload_thread(self, endpoint_url: str):
        log_queue = self._queue
        stop_logging_upload = Event()

        def _thread_main():
            _session = requests.Session()

            while not stop_logging_upload.is_set():
                entry = log_queue.get()
                if entry is None:
                    return  # stop signal
                if not entry:
                    continue  # skip uploading empty log line

                with contextlib.suppress(Exception):
                    _session.post(endpoint_url, data=entry, timeout=3)

        log_upload_thread = Thread(target=_thread_main, daemon=True)
        log_upload_thread.start()

        def _thread_exit():
            stop_logging_upload.set()
            log_queue.put_nowait(None)
            log_upload_thread.join(6)

        atexit.register(_thread_exit)


def configure_logging() -> None:
    """Configure logging with http handler."""
    # ------ suppress logging from non-first-party modules ------ #
    # NOTE: force to reload the basicConfig, this is for overriding setting
    #       when launching subprocess.
    # NOTE: for the root logger, set to CRITICAL to filter away logs from other
    #       external modules unless reached CRITICAL level.
    logging.basicConfig(level=logging.CRITICAL, format=cfg.LOG_FORMAT, force=True)

    # ------ configure each sub loggers and attach ota logging handler ------ #
    log_upload_handler = None
    if logging_upload_endpoint := proxy_info.logging_server:
        logging_upload_endpoint = f"{str(logging_upload_endpoint).strip('/')}/"

        log_upload_handler = _LogTeeHandler()
        fmt = logging.Formatter(fmt=cfg.LOG_FORMAT)
        log_upload_handler.setFormatter(fmt)

        # star the logging thread
        log_upload_endpoint = urljoin(logging_upload_endpoint, ecu_info.ecu_id)
        log_upload_handler.start_upload_thread(log_upload_endpoint)

    for logger_name, loglevel in cfg.LOG_LEVEL_TABLE.items():
        _logger = logging.getLogger(logger_name)
        _logger.setLevel(loglevel)
        if log_upload_handler:
            _logger.addHandler(log_upload_handler)
