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
"""Runtime configurable configs for otaclient."""

from __future__ import annotations

import json
import logging
from typing import Dict, Literal

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from otaclient.configs._cfg_consts import cfg_consts

logger = logging.getLogger(__name__)

ENV_PREFIX = "OTACLIENT_"
LOG_LEVEL_LITERAL = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class _OTAClientSettings(BaseModel):
    # ------ OTA session setting ------ #
    SESSION_WD_TMPFS_SIZE_IN_MB: int = 700  # MB

    #
    # ------ logging settings ------ #
    #
    DEFAULT_LOG_LEVEL: LOG_LEVEL_LITERAL = "INFO"
    LOG_LEVEL_TABLE: Dict[str, LOG_LEVEL_LITERAL] = {
        "ota_metadata": "INFO",
        "otaclient": "INFO",
        "otaclient_api": "INFO",
        "otaclient_common": "INFO",
        "ota_proxy": "INFO",
    }

    @property
    def LOG_FORMAT(self) -> str:
        """Generate JSON log format string dynamically."""
        log_fields = {
            "timestamp": "%(asctime)s",
            "level": "%(levelname)s",
            "logger": "%(name)s",
            "function": "%(funcName)s",
            "line": "%(lineno)d",
            "message": "%(message)s",
        }
        return json.dumps(log_fields, separators=(",", ":"))

    #
    # ------ downloading settings ------ #
    #
    DOWNLOAD_RETRY_PRE_REQUEST: int = 3
    DOWNLOAD_BACKOFF_MAX: int = 3  # seconds
    DOWNLOAD_BACKOFF_FACTOR: float = 0.1  # seconds

    DOWNLOAD_THREADS: int = 6
    MAX_CONCURRENT_DOWNLOAD_TASKS: int = 128
    DOWNLOAD_INACTIVE_TIMEOUT: int = 5 * 60  # seconds
    MAX_RETRY_ON_ENTRY_COUNT: int = 300  # counts. almost 5 minutes
    CLIENT_UPDATE_TIMEOUT: int = 5 * 60  # seconds

    #
    # ------ create standby settings ------ #
    #
    MAX_CONCURRENT_PROCESS_FILE_TASKS: int = 256
    MAX_PROCESS_FILE_THREAD: int = 6
    CREATE_STANDBY_RETRY_MAX: int = 1024

    #
    # ------ IO settings ------ #
    #
    PROCESS_FILES_REPORT_INTERVAL: int = 3  # seconds
    CHUNK_SIZE: int = 1024 * 1024  # 1MiB
    READ_CHUNK_SIZE: int = 4 * 1024 * 1024  # 4MiB

    FSTRIM_AT_OTA: bool = True
    FSTRIM_AT_OTA_TIMEOUT: int = 30  # seconds
    FSTRIM_AT_OTACLIENT_STARTUP: bool = True
    FSTRIM_AT_OTACLIENT_STARTUP_TIMEOUT: int = 360  # 6mins

    #
    # ------ delta calculation ------ #
    #
    DELTA_SIZE_THRESHOLD_ENABLE_ACTIVE_SLOT_COPY: int = 5 * 1024**3  # 5GiB


class _MultipleECUSettings(BaseModel):
    # The timeout of waiting sub ECU acks the OTA request.
    WAITING_SUBECU_ACK_REQ_TIMEOUT: int = 6

    # The timeout of waiting sub ECU responds to status API request
    QUERYING_SUBECU_STATUS_TIMEOUT: int = 6

    # The ECU status storage will summarize the stored ECUs' status report
    # and generate overall status report for all ECUs every <INTERVAL> seconds.
    OVERALL_ECUS_STATUS_UPDATE_INTERVAL: int = 6  # seconds

    # If ECU has been disconnected longer than <TIMEOUT> seconds, it will be
    # treated as UNREACHABLE, and will not be counted when generating overall
    # ECUs status report.
    # NOTE: unreachable_timeout should be larger than
    #       downloading_group timeout
    ECU_UNREACHABLE_TIMEOUT: int = 20 * 60  # seconds

    # Otaproxy should not be shutdowned with less than <INTERVAL> seconds
    # after it just starts to prevent repeatedly start/stop cycle.
    OTAPROXY_MINIMUM_SHUTDOWN_INTERVAL: int = 1 * 60  # seconds

    # When any ECU acks update request, this ECU will directly set the overall ECU status
    # to any_in_update=True, any_requires_network=True, all_success=False, to prevent
    # pre-mature overall ECU status changed caused by child ECU delayed ack to update request.
    #
    # This pre-set overall ECU status will be kept for <KEEP_TIME> seconds.
    # This value is expected to be larger than the time cost for subECU acks the OTA request.
    PAUSED_OVERALL_ECUS_STATUS_CHANGE_ON_UPDATE_REQ_ACKED: int = 60  # seconds


class _OTAProxySettings(BaseModel):
    OTAPROXY_ENABLE_EXTERNAL_CACHE: bool = True
    EXTERNAL_CACHE_DEV_MOUNTPOINT: str = f"{cfg_consts.MOUNT_SPACE}/external_cache"


class ConfigurableSettings(_OTAClientSettings, _MultipleECUSettings, _OTAProxySettings):
    """otaclient runtime configuration settings."""


def set_configs() -> ConfigurableSettings:
    try:

        class _SettingParser(ConfigurableSettings, BaseSettings):
            model_config = SettingsConfigDict(
                validate_default=True,
                env_prefix=ENV_PREFIX,
            )

        _parsed_setting = _SettingParser()
        return ConfigurableSettings.model_construct(**_parsed_setting.model_dump())
    except Exception as e:
        logger.error(f"failed to parse otaclient configurable settings: {e!r}")
        logger.warning("use default settings ...")
        return ConfigurableSettings()
