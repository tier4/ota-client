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
import os
import requests
import time
import yaml
from queue import Queue
from threading import Thread
from requests.adapters import HTTPAdapter
from urllib.parse import urljoin, urlparse
from urllib3.util.retry import Retry

from otaclient import otaclient_package_name
from .configs import config as cfg, logging_config


# NOTE: EcuInfo imports this log_setting so independent get_ecu_id are required.
def get_ecu_id():
    try:
        with open(cfg.ECU_INFO_FPATH) as f:
            ecu_info = yaml.load(f, Loader=yaml.SafeLoader)
            return ecu_info["ecu_id"]
    except Exception:
        return "autoware"


def get_logger(name: str) -> logging.Logger:
    """Helper method to get logger with name."""
    logger = logging.getLogger(name)
    logger.setLevel(
        logging_config.LOG_LEVEL_TABLE.get(__name__, logging_config.LOGGING_LEVEL)
    )
    return logger


def configure_logging(loglevel: int, *, logging_url_path: str):
    """Configure logging with HTTP handler.

    A aws-iot-logging server will be launched and serve a HTTP endpoint at
        <http_logging_url>/<hogging_url_path>, this function configures to
        upload the otaclient logger's logging to this endpoint.

    Args:
        loglevel: the logging level for the configured root logger.
        logging_url_path: the path component in logging upload URL.
    """
    # configure the root logger
    # NOTE: force to reload the basicConfig, this is for overriding setting
    #       when launching subprocess.
    # NOTE: for the root logger, set to CRITICAL to filter away logs from other
    #       external modules unless reached CRITICAL level.
    logging.basicConfig(
        level=logging.CRITICAL, format=logging_config.LOG_FORMAT, force=True
    )
    # NOTE: set the <loglevel> to the otaclient package root logger
    _otaclient_logger = logging.getLogger(otaclient_package_name)
    _otaclient_logger.setLevel(loglevel)

    # if http_logging is enabled, attach the http handler to
    # the otaclient package root logger
    if http_logging_url := os.environ.get("HTTP_LOGGING_SERVER"):
        logging_url = urljoin(f"{http_logging_url.rstrip('/')}/", logging_url_path)

        ch = CustomHttpHandler(url=logging_url)
        ch.setFormatter(logging.Formatter(fmt=logging_config.LOG_FORMAT))

        # NOTE: "otaclient" logger will be the root logger for all loggers name
        #       starts with "otaclient.", and the settings will affect its child loggers.
        #       For example, settings for "otaclient" logger will also be effective to
        #       "otaclient.app.*" logger and "otaclient.ota_proxy.*" logger.
        _otaclient_logger.addHandler(ch)


class CustomHttpHandler(logging.Handler):
    """
    Moved from aws_iot_log_server.custom_http_handler module.
    """

    # https://stackoverflow.com/questions/62957814/python-logging-log-data-to-server-using-the-logging-module
    def __init__(self, url: str, timeout=3.0, interval=0.1, *, max_queue_len=4096):
        """
        only method post is supported
        secure(=https) is not supported
        """
        super().__init__()
        _parsed_url = urlparse(url)
        self.url = _parsed_url._replace(scheme="http").geturl()

        self.timeout = timeout
        self.interval = interval
        self.queue = Queue(maxsize=max_queue_len)

        self.thread = Thread(target=self._start, daemon=True)
        self.thread.start()

    def emit(self, record):
        """Get a record from logging module and put it into self.queue."""
        _queue = self.queue
        with _queue.mutex:
            if 0 < _queue.maxsize <= _queue._qsize():
                _queue._get()  # drop one oldest item from list
            _queue._put(self.format(record))

    def _start(self):
        session = requests.Session()

        # FIXME: bump to requests with urllib3 v2.
        retries = Retry(total=5, backoff_factor=1)
        # NOTE: for urllib3 version below 2.0, there is no way to set
        #       backoff_max when instanitiate Retry object.
        Retry.DEFAULT_BACKOFF_MAX = 10  # type: ignore
        session.mount("http://", HTTPAdapter(max_retries=retries))

        while True:
            log_entry = self.queue.get()
            try:
                with session.post(
                    url=self.url,
                    data=log_entry,
                    timeout=self.timeout,
                ):
                    pass
            except Exception:
                pass
            finally:
                time.sleep(self.interval)
