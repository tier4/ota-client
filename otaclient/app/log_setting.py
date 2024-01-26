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

from otaclient import otaclient_package_name
from .configs import logging_config, proxy_info


def get_logger(name: str) -> logging.Logger:
    """Helper method to get logger with name."""
    logger = logging.getLogger(name)
    logger.setLevel(
        logging_config.LOG_LEVEL_TABLE.get(__name__, logging_config.LOGGING_LEVEL)
    )
    return logger


def configure_logging(loglevel: int, *, http_logging_url: str) -> None:
    """Configure logging with http handler.

    Args:
        loglevel(int): the loglevel to the configured logger.
        http_logging_url(str): the path component of the logging dest URL.
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
    # NOTE(20240126): now proxy_info module can parse the http_logging setting.
    if http_logging_host := proxy_info.logging_server:
        from otaclient.aws_iot_log_server import CustomHttpHandler

        ch = CustomHttpHandler(host=http_logging_host, url=http_logging_url)
        fmt = logging.Formatter(fmt=logging_config.LOG_FORMAT)
        ch.setFormatter(fmt)

        # NOTE: "otaclient" logger will be the root logger for all loggers name
        #       starts with "otaclient.", and the settings will affect its child loggers.
        #       For example, settings for "otaclient" logger will also be effective to
        #       "otaclient.app.*" logger and "otaclient.ota_proxy.*" logger.
        _otaclient_logger.addHandler(ch)
