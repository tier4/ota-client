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

import logging
import os
import yaml

from .configs import config as cfg

# NOTE: EcuInfo imports this log_setting so independent get_ecu_id are required.
def get_ecu_id():
    try:
        with open(cfg.ECU_INFO_FILE) as f:
            ecu_info = yaml.load(f, Loader=yaml.SafeLoader)
            return ecu_info["ecu_id"]
    except Exception:
        return "autoware"


def get_logger(name: str, loglevel: int) -> logging.Logger:
    """Helper method to get logger with name and loglevel."""
    logger = logging.getLogger(name)
    logger.setLevel(loglevel)
    return logger


def configure_logging(loglevel: int, *, http_logging_url: str):
    """Configure logging with http handler."""
    # configure the root logger
    # NOTE: force to reload the basicConfig, this is for overriding setting
    #       when launching subprocess.
    # NOTE: for the root logger, set to CRITICAL to filter away logs from other
    #       external modules unless reached CRITICAL level.
    logging.basicConfig(level=logging.CRITICAL, format=cfg.LOG_FORMAT, force=True)
    # NOTE: set the <loglevel> to the otaclient package root logger
    _otaclient_logger = logging.getLogger("otaclient")
    _otaclient_logger.setLevel(loglevel)

    # if http_logging is enabled, attach the http handler to
    # the otaclient package root logger
    if http_logging_host := os.environ.get("HTTP_LOGGING_SERVER"):
        from otaclient.aws_iot_log_server import CustomHttpHandler

        ch = CustomHttpHandler(host=http_logging_host, url=http_logging_url)
        fmt = logging.Formatter(fmt=cfg.LOG_FORMAT)
        ch.setFormatter(fmt)

        # NOTE: "otaclient" logger will be the root logger for all loggers name
        #       starts with "otaclient.", and the settings will affect its child loggers.
        #       For example, settings for "otaclient" logger will also be effective to
        #       "otaclient.app.*" logger and "otaclient.ota_proxy.*" logger.
        _otaclient_logger.addHandler(ch)
