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
from dataclasses import dataclass

from _otaclient_version import __version__

from otaclient.configs.cfg import (
    ecu_info,
)

logger = logging.getLogger(__name__)


class OTAMetrics:
    @dataclass
    class OTAMetricsData:
        """
        Dataclass for storing metrics data.
        These data are collected during the OTA update process, converted to JSON, and expected to be used in subscription filters.
        Thus, flatten structure is used for easy access.
        """

        # Date
        delta_calculation_start_timestamp: int = 0
        delta_calculation_finish_timestamp: int = 0
        download_start_timestamp: int = 0
        download_finish_timestamp: int = 0
        apply_update_start_timestamp: int = 0
        apply_update_finish_timestamp: int = 0
        post_update_start_timestamp: int = 0
        post_update_finish_timestamp: int = 0

        # ECU and Firmware
        ecu_id: str = ecu_info.ecu_id
        current_firmware_version: str = ""
        target_firmware_version: str = ""

        # OTA Client
        otaclient_version: str = __version__

        # Status
        failure_type: str = ""
        failure_reason: str = ""
        failure_traceback: str = ""
        failed_at_phase: str = ""

        # Metrics
        ota_image_total_files_num: int = 0
        ota_image_total_files_size: int = 0
        ota_image_total_resources_num: int = 0
        ota_image_total_non_regular_files_num: int = 0
        ota_image_total_directories_num: int = 0
        delta_reuse_resources_num: int = 0
        delta_reuse_resources_size: int = 0
        delta_download_resources_num: int = 0
        delta_download_resources_size: int = 0
        delta_remove_resources_num: int = 0
        processed_regular_files_num: int = 0
        processed_resources_num: int = 0
        downloaded_bytes: int = 0
        downloaded_errors: int = 0

    def __init__(self):
        self.data = self.OTAMetricsData()

    def update(self, **kwargs):
        """
        Updates the metrics data with the specified key-value pairs.

        :param kwargs: Key-value pairs to update in metrics_data.
        """
        for key, value in kwargs.items():
            if hasattr(self.data, key):
                setattr(self.data, key, value)
            else:
                logger.warning(f"Key {key} is not found in metrics_data.")

    def publish(self):
        """
        Publishes the metrics data to the metrics server.
        """
        # publishing the metrics via logging
        # logger.info(json.dumps(self.metrics_data), extra={"log_type": LogType.METRICS})

        logger.info(json.dumps(self.data))
