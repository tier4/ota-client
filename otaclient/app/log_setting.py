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

from .configs import config as cfg


def configure_logging(loglevel: int, *, http_logging_url: str):
    """Configure logging with http handler."""
    # configure the root logger
    logging.basicConfig(level=loglevel, format=cfg.LOG_FORMAT)

    # if http_logging is enabled, attach the http handler to the root logger
    if http_logging_host := os.environ.get("HTTP_LOGGING_SERVER"):
        from otaclient.aws_iot_log_server import CustomHttpHandler

        ch = CustomHttpHandler(host=http_logging_host, url=http_logging_url)
        fmt = logging.Formatter(fmt=cfg.LOG_FORMAT)
        ch.setFormatter(fmt)

        # NOTE: using getLogger without any argument will get the root logger
        root_logger = logging.getLogger()
        root_logger.addHandler(ch)
