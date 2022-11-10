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

logging.basicConfig(level=cfg.LOG_LEVEL, format=cfg.LOG_FORMAT)
if http_logging_host := os.environ.get("HTTP_LOGGING_SERVER"):
    from otaclient.aws_iot_log_server import CustomHttpHandler

    h = CustomHttpHandler(host=http_logging_host, url="ota_proxy")
    fmt = logging.Formatter(fmt=cfg.LOG_FORMAT)
    h.setFormatter(fmt)
    logging.addHandler(h)
