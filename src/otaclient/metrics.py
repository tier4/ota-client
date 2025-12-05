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

import json
import logging
from dataclasses import asdict, dataclass
from enum import Enum

from _otaclient_version import __version__

from otaclient._logging import LogType
from otaclient.configs.cfg import ecu_info

logger = logging.getLogger(__name__)


class OTAImageFormat(str, Enum):
    """OTA image format types."""

    LEGACY = "legacy"
    V1 = "v1"


@dataclass
class OTAMetricsSharedMemoryData:
    """
    Dataclass for storing metrics data in shared memory.
    """

    # Cache
    cache_total_requests: int = 0
    cache_cdn_hits: int = 0
    cache_external_hits: int = 0
    cache_external_nfs_hits: int = 0
    cache_local_hits: int = 0


@dataclass
class OTAMetricsData:
    """
    Dataclass for storing metrics data.
    These data are collected during the OTA update process, converted to JSON, and expected to be used in subscription filters.
    Thus, flatten structure is used for easy access.
    """

    # Date
    initializing_start_timestamp: int = 0
    processing_metadata_start_timestamp: int = 0
    delta_calculation_start_timestamp: int = 0
    download_start_timestamp: int = 0
    apply_update_start_timestamp: int = 0
    post_update_start_timestamp: int = 0
    finalizing_update_start_timestamp: int = 0
    reboot_start_timestamp: int = 0

    # ECU and Firmware
    ecu_id: str = ecu_info.ecu_id
    request_id: str = ""
    session_id: str = ""
    current_firmware_version: str = ""
    standby_firmware_version: str = ""
    target_firmware_version: str = ""

    # OTA Client
    otaclient_version: str = __version__

    # Status
    failure_type: str = ""
    failure_reason: str = ""
    failed_status: str = ""

    # Mode
    use_inplace_mode: bool = False

    # Image format
    ota_image_format: str = ""

    # Metrics
    ota_image_total_files_size: int = 0
    ota_image_total_regulars_num: int = 0
    ota_image_total_directories_num: int = 0
    ota_image_total_symlinks_num: int = 0
    delta_download_files_num: int = 0
    delta_download_files_size: int = 0
    downloaded_bytes: int = 0
    downloaded_errors: int = 0

    # Bootloader type
    bootloader_type: str = ""

    # Cache settings
    enable_local_ota_proxy_cache: bool = False

    # Cache metrics
    cache_total_requests: int = 0
    cache_cdn_hits: int = 0
    cache_external_hits: int = 0
    cache_local_hits: int = 0

    def __post_init__(self):
        # this variable will not be included in data fields
        self._already_published = False

    def shm_merge(self, shm_data: OTAMetricsSharedMemoryData | None) -> None:
        """
        Merges OTAMetricsSharedMemoryData instance into this one.
        This is useful for combining metrics from different phases of the OTA process.
        """
        try:
            if shm_data is None:
                return

            # Merge the shared memory data into this instance
            for field in asdict(self):
                if hasattr(shm_data, field):
                    setattr(self, field, getattr(shm_data, field))
        except Exception as e:
            logger.warning(f"Failed to read from shared memory: {e}")

    def publish(self):
        """
        Publishes the metrics data to the metrics server.
        """
        if self._already_published:
            # metrics data has already been published.
            return

        try:
            logger.info(json.dumps(asdict(self)), extra={"log_type": LogType.METRICS})
            self._already_published = True
        except Exception as e:
            logger.error(f"Failed to publish metrics: {e}")
