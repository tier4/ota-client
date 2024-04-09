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
"""otaclient runtime configs.

This module supports otaclient runtime behavior configs via environmental variables.

<config> can be set by exporting OTACLIENT_<config> variable.
"""


from __future__ import annotations
from enum import Enum
from logging import INFO
from typing import Dict

from pydantic import Field, IPvAnyAddress
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing_extensions import Annotated

Port = Annotated[int, Field(ge=1, le=65535)]
ENV_PREFIX = "OTACLIENT_"


class CreateStandbyMechanism(str, Enum):
    LEGACY = "legacy"  # deprecated and removed
    REBUILD = "rebuild"  # current default
    IN_PLACE = "in_place"  # not yet implemented


class AppConfigs(BaseSettings):

    model_config = SettingsConfigDict(
        env_file=ENV_PREFIX,
        frozen=True,
        validate_default=True,
    )

    OTA_TMP_DNAME: str = "ota_tmp"
    """name of OTA used temp folder.
    Default: ota_tmp.
    """
    CERTS_DPATH: str = "/opt/ota/client/certs"

    # ------ otaclient logging setting ------ #
    # TODO: in the future use a config yaml file to define logging
    #   settings.
    DEFAULT_LOG_LEVEL = INFO
    LOG_LEVEL_TABLE: Dict[str, int] = {
        "otaclient.app.boot_control.cboot": INFO,
        "otaclient.app.boot_control.grub": INFO,
        "otaclient.app.ota_client": INFO,
        "otaclient.app.ota_client_service": INFO,
        "otaclient.app.ota_client_stub": INFO,
        "otaclient.app.ota_metadata": INFO,
        "otaclient.app.downloader": INFO,
        "otaclient.app.main": INFO,
    }
    LOG_FORMAT = (
        "[%(asctime)s][%(levelname)s]-%(name)s:%(funcName)s:%(lineno)d,%(message)s"
    )

    #
    # ------ otaclient OTA service API config ------ #
    #
    OTA_API_SERVER_ADDRESS: IPvAnyAddress = Field(default="127.0.0.1")
    OTA_API_SERVER_PORT: Port = 50051
    CLIENT_CALL_PORT: Port = 50051

    #
    # ------ otaproxy server config ------ #
    #
    OTA_PROXY_LISTEN_ADDRESS: IPvAnyAddress = Field(default="0.0.0.0")
    OTA_PROXY_LISTEN_PORT: Port = 8082

    #
    # ------ otaclient logging server config ------ #
    #
    LOGGING_SERVER_ADDRESS: IPvAnyAddress = Field(default="127.0.0.1")
    LOGGING_SERVER_PORT: Port = 8083

    #
    # ------ otaclient runtime behavior setting ------ #
    #

    # --- request dispatch settings --- #
    WAITING_SUBECU_ACK_REQ_TIMEOUT: int = 6
    QUERYING_SUBECU_STATUS_TIMEOUT: int = 30
    LOOP_QUERYING_SUBECU_STATUS_INTERVAL: int = 10

    # --- file I/O settings --- #
    CHUNK_SIZE: int = 1 * 1024 * 1024  # 1MB
    LOCAL_CHUNK_SIZE: int = 4 * 1024 * 1024  # 4MB

    #
    # --- download settings for single download task --- #
    #
    DOWNLOAD_RETRY: int = 3
    DOWNLOAD_BACKOFF_MAX: int = 3  # seconds
    DOWNLOAD_BACKOFF_FACTOR: float = 0.1  # seconds

    #
    # --- downloader settings --- #
    #
    MAX_DOWNLOAD_THREAD: Annotated[int, Field(le=16)] = 7
    DOWNLOADER_CONNPOOL_SIZE_PER_THREAD: Annotated[int, Field(le=64)] = 20

    #
    # --- download settings for the whole download tasks group --- #
    #
    MAX_CONCURRENT_DOWNLOAD_TASKS: Annotated[int, Field(le=1024)] = 128
    DOWNLOAD_GROUP_INACTIVE_TIMEOUT: int = 5 * 60  # seconds
    """
    if retry keeps failing without any success in
        DOWNLOAD_GROUP_INACTIVE_TIMEOUT time, failed the whole
        download task group and raise NETWORK OTA error."""

    DOWNLOAD_GROUP_BACKOFF_MAX: int = 12  # seconds
    DOWNLOAD_GROUP_BACKOFF_FACTOR: int = 1  # seconds

    #
    # --- stats collector setting --- #
    #
    STATS_COLLECT_INTERVAL: int = 1  # second

    #
    # --- create standby setting --- #
    #
    # now only REBUILD mode is available
    STANDBY_CREATION_MODE: CreateStandbyMechanism = CreateStandbyMechanism.REBUILD
    MAX_CONCURRENT_PROCESS_FILE_TASKS: Annotated[int, Field(le=2048)] = 256
    CREATE_STANDBY_RETRY_MAX: int = 3
    CREATE_STANDBY_BACKOFF_FACTOR: int = 1
    CREATE_STANDBY_BACKOFF_MAX: int = 6

    #
    # --- ECU status polling setting, otaproxy dependency managing --- #
    #
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
    KEEP_OVERALL_ECUS_STATUS_ON_ANY_UPDATE_REQ_ACKED: int = 60  # seconds

    # Active status polling interval, when there is active OTA update in the cluster.
    ACTIVE_INTERVAL: int = 1  # second

    # Idle status polling interval, when ther is no active OTA updaste in the cluster.
    IDLE_INTERVAL: int = 10  # seconds

    #
    # --- default version str --- #
    #
    # The string return in status API firmware_version field if version is unknown.
    DEFAULT_VERSION_STR: str = ""


app_config = AppConfigs()
