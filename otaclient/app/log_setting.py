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

import atexit
import logging
import os
import yaml
from queue import Queue
from threading import Event, Thread
from urllib.parse import urljoin

import requests

import otaclient
from .configs import config as cfg


# NOTE: EcuInfo imports this log_setting so independent get_ecu_id are required.
def get_ecu_id():
    try:
        with open(cfg.ECU_INFO_FILE) as f:
            ecu_info = yaml.load(f, Loader=yaml.SafeLoader)
            return ecu_info["ecu_id"]
    except Exception:
        return "autoware"


class _LogTeeHandler(logging.Handler):
    """Implementation of teeing local logs to a remote otaclient-iot-logger server."""

    def __init__(self, max_backlog: int = 2048) -> None:
        super().__init__()
        self._queue: Queue[str | None] = Queue(maxsize=max_backlog)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._queue.put_nowait(self.format(record))
        except Exception:
            pass

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

                try:
                    _session.post(endpoint_url, data=entry, timeout=3)
                except Exception:
                    pass

        log_upload_thread = Thread(target=_thread_main, daemon=True)
        log_upload_thread.start()

        def _thread_exit():
            stop_logging_upload.set()
            log_queue.put_nowait(None)
            log_upload_thread.join(6)

        atexit.register(_thread_exit)


def configure_logging():
    """Configure logging with http handler."""
    # configure the root logger
    # NOTE: force to reload the basicConfig, this is for overriding setting
    #       when launching subprocess.
    # NOTE: for the root logger, set to CRITICAL to filter away logs from other
    #       external modules unless reached CRITICAL level.
    logging.basicConfig(level=logging.CRITICAL, format=cfg.LOG_FORMAT, force=True)
    # NOTE: set the <loglevel> to the otaclient package root logger
    _otaclient_logger = logging.getLogger(otaclient.__name__)
    _otaclient_logger.setLevel(cfg.DEFAULT_LOG_LEVEL)

    # configure each sub loggers
    for _module_name, _log_level in cfg.LOG_LEVEL_TABLE.items():
        _logger = logging.getLogger(_module_name)
        _logger.setLevel(_log_level)

    # NOTE(20240306): for only god knows reason, although in proxy_info.yaml,
    #   the logging_server field is assigned with an URL, and otaclient
    #   expects an URL, the run.sh from autoware_ecu_system_setup pass in
    #   HTTP_LOGGING_SERVER with URL schema being removed!?
    # NOTE: I will do a quick fix here as I don't want to touch autoware_ecu_system_setup
    #   for now, leave it in the future.
    if iot_logger_url := os.environ.get("HTTP_LOGGING_SERVER"):
        # special treatment for not-a-URL passed in by run.sh
        # note that we only support http, the proxy_info.yaml should be properly setup.
        if not (iot_logger_url.startswith("http") or iot_logger_url.startswith("HTTP")):
            iot_logger_url = f"http://{iot_logger_url.strip('/')}"
        iot_logger_url = f"{iot_logger_url.strip('/')}/"

        ch = _LogTeeHandler()
        fmt = logging.Formatter(fmt=cfg.LOG_FORMAT)
        ch.setFormatter(fmt)

        # star the logging thread
        log_upload_endpoint = urljoin(iot_logger_url, get_ecu_id())
        ch.start_upload_thread(log_upload_endpoint)

        # NOTE: "otaclient" logger will be the root logger for all loggers name
        #       starts with "otaclient.", and the settings will affect its child loggers.
        #       For example, settings for "otaclient" logger will also be effective to
        #       "otaclient.app.*" logger and "otaclient.ota_proxy.*" logger.
        _otaclient_logger.addHandler(ch)
