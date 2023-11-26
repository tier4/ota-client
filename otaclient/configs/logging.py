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
"""otaclient logging configs."""


from __future__ import annotations
import logging
from pydantic import AfterValidator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Dict
from typing_extensions import Annotated

from otaclient._utils.logging import check_loglevel
from . import ENV_PREFIX


class LoggingSetting(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        frozen=True,
        validate_default=True,
    )

    LOGGING_LEVEL: Annotated[int, AfterValidator(check_loglevel)] = logging.INFO
    LOG_LEVEL_TABLE: Dict[str, Annotated[int, AfterValidator(check_loglevel)]] = {
        "otaclient.app.boot_control.cboot": LOGGING_LEVEL,
        "otaclient.app.boot_control.grub": LOGGING_LEVEL,
        "otaclient.app.ota_client": LOGGING_LEVEL,
        "otaclient.app.ota_client_service": LOGGING_LEVEL,
        "otaclient.app.ota_client_stub": LOGGING_LEVEL,
        "otaclient.app.ota_metadata": LOGGING_LEVEL,
        "otaclient.app.downloader": LOGGING_LEVEL,
        "otaclient.app.main": LOGGING_LEVEL,
    }
    LOG_FORMAT: str = (
        "[%(asctime)s][%(levelname)s]-%(name)s:%(funcName)s:%(lineno)d,%(message)s"
    )


logging_config = LoggingSetting()
