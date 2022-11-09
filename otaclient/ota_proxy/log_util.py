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


import os
import logging

from .config import config as cfg

_logger = None


def get_logger(name: str, level: int) -> logging.Logger:
    global _logger
    if _logger:
        return _logger
    _logger = _get_logger(name, level)
    return _logger


def _get_logger(name: str, level: int) -> logging.Logger:
    logging.basicConfig(format=cfg.LOG_FORMAT)
    logger = logging.getLogger(name)
    logger.setLevel(level)
    http_logging_host = os.environ.get("HTTP_LOGGING_SERVER")
    if http_logging_host:
        from otaclient.aws_iot_log_server import CustomHttpHandler

        h = CustomHttpHandler(host=http_logging_host, url="ota_proxy")
        fmt = logging.Formatter(fmt=cfg.LOG_FORMAT)
        h.setFormatter(fmt)
        logger.addHandler(h)

    return logger
