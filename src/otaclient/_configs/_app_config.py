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
from typing import Dict, Literal, Union

from otaclient_common.typing import LoggingLevel, NetworkPort

from ._common import BaseFixedConfig
from ._consts import CreateStandbyMechanism


class CommonOTAClientConfig(BaseFixedConfig):
    """Common configuration for normal users."""

    # ------ Download related ------ #
    DOWNLOAD_THREAD: int = 6
    DOWNLOAD_IDLE_TIMEOUT: int = 5 * 60  # seconds
    """
    If the download keeps stuck longer than <DOWNLOAD_IDLE_TIMEOUT> seconds,
        the OTA downloading will fail and breakout the whole OTA.
    """
    DOWNLOAD_CONCURRENCY: int = 128  # tasks in backlog

    # ------ OTA update progress collecting interval ------ #
    UPDATE_STATS_COLLECT_INTERVAL: int = 1  # second

    # ------ Local delta calculation & apply update related ------ #
    FILE_PROCESS_THREAD: int = 6
    FILE_PROCESS_CONCURRENCY: int = 512  # files simultaneously


class AdvancedOTAClientConfiguration(BaseFixedConfig):
    """For developer or advanced users to further control otaclient's behavior.

    Change the settings CAREFULLY, incorrect or inproper values will result in otaclient
        broken or not behaving as expected.
    """

    OTACLIENT_PID_FPATH: str = "/run/otaclient.pid"
    OTACLIENT_INSTALLATION_DPATH: str = "/opt/ota/client"
    OTA_CERTS_DPATH: str = "/opt/ota/client/certs"

    # ------ External cache source support for otaproxy ------ #
    EXTERNAL_CACHE_DEV_FSLABEL: str = "ota_cache_src"

    # ------ IO setting ------ #
    CHUNK_SIZE: int = 1 * 1024 * 1024  # 1MB
    LOCAL_CHUNK_SIZE: int = 4 * 1024 * 1024  # 4MB

    DEFAULT_VERSION_STR: str = ""
    """Default firmware version string to be reported in status API response."""

    # ------ Runtime use folders and paths ------ #
    OTACLIENT_RUN_DIR: str = "/run/otaclient"
    OTACLIENT_MOUNT_SPACE: str = "/run/otaclient/mount"
    OTACLIENT_CONFIGURATION_DIR: str = "/boot/ota"
    """Where the otaclient configuration files(ecu_info.yaml and proxy_info.yaml) stored to."""
    OTA_IMAGE_METADATA_DIR: str = "/opt/ota/image-meta"
    """Where to store the copy of OTA image metadata in standby slot."""

    # ------ Container mode ------ #
    HOST_ROOTFS: Union[Literal["/"], str] = "/"
    """The host rootfs mount point.

    When set to value other than '/', otaclient will activate the container running mode,
        the host rootfs is expected to be mounted at <HOST_ROOTFS>.
    """

    # ------ grpc OTA API server config ------ #
    API_SERVER_PORT: NetworkPort = 50051

    # ------ create standby related ------ #
    CREATE_STANDBY_METHOD: CreateStandbyMechanism = CreateStandbyMechanism.REBUILD
    CREATE_STANDBY_RETRY_MAX: int = 1024

    # ------ DEBUG mode ------ #
    DEBUG_MODE: bool = False
    """Enable debug mode globally.
    
    Currently this flag will enable:
    1. detailed failure trackback in status API response.
    """

    # ------ Multiple ECU update related ------ #
    # The values are set to tolerate high latency between sub ECUs.
    OTAPROXY_MINIMUM_SHUTDOWN_INTERVAL: int = 60  # seconds
    OVERALL_ECUS_STATUS_UPDATE_INTERVAL: int = 6
    ECU_UNREACHABLE_TIMEOUT: int = 20 * 60  # seconds
    """
    If ECU has been disconnected longer than <TIMEOUT> seconds, it will be
    treated as UNREACHABLE, and will not be counted when generating overall
    ECUs status report.
    NOTE: unreachable_timeout should be larger than
          downloading_group timeout
    """
    WAITING_SUBECU_ACK_REQ_TIMEOUT: int = 12  # seconds
    ECU_STATUS_PULLING_INTERVAL: int = 1  # second
    KEEP_OVERALL_ECUS_STATUS_ON_ANY_UPDATE_REQ_ACKED = 60  # seconds
    ACTIVE_STATUS_POLL_INTERVAL = 1  # second
    IDLE_STATUS_POLL_INTERVAL = 10  # seconds


class LoggingConfig(BaseFixedConfig):
    LOG_LEVEL_TABLE: Dict[str, LoggingLevel] = {
        "otaclient": logging.INFO,
        "otaclient_common": logging.INFO,
        "otaclient_api": logging.INFO,
        "ota_proxy": logging.INFO,
        "ota_metadata": logging.INFO,
    }
    LOG_FORMAT: str = (
        "[%(asctime)s][%(levelname)s]-%(name)s:%(funcName)s:%(lineno)d,%(message)s"
    )
