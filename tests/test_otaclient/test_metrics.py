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
"""Test module for OTA client metrics."""

from __future__ import annotations

import json
from unittest.mock import patch

from _otaclient_version import __version__
from otaclient import metrics
from otaclient._logging import LogType
from otaclient.configs.cfg import ecu_info

MODULE = metrics.__name__


class TestOTAMetricsData:
    """Test class for OTAMetricsData functionality."""

    def test_init(self):
        """Test initialization of OTAMetricsData."""
        ota_metrics = metrics.OTAMetricsData()
        assert ota_metrics.otaclient_version == __version__
        assert ota_metrics.ecu_id == ecu_info.ecu_id
        assert ota_metrics._already_published is False

    @patch("otaclient.metrics.logger")
    def test_shm_merge_success(self, mock_logger):
        """Test successful merge of shared memory data."""
        ota_metrics = metrics.OTAMetricsData()

        # Create mock shared memory data
        shm_metrics = metrics.OTAMetricsSharedMemoryData()
        shm_metrics.cache_total_requests = 100
        shm_metrics.cache_external_hits = 50
        shm_metrics.cache_local_hits = 30

        # Test the merge
        ota_metrics.shm_merge(shm_metrics)

        # Verify the data was merged
        assert ota_metrics.cache_total_requests == 100
        assert ota_metrics.cache_external_hits == 50
        assert ota_metrics.cache_local_hits == 30

    @patch("otaclient.metrics.logger")
    def test_shm_merge_no_data(self, mock_logger):
        """Test merge when shared memory returns None."""
        ota_metrics = metrics.OTAMetricsData()

        # Store initial values
        initial_cache_requests = ota_metrics.cache_total_requests
        initial_cache_hits = ota_metrics.cache_external_hits

        # Test the merge with None
        ota_metrics.shm_merge(None)

        # Verify the data was not changed
        assert ota_metrics.cache_total_requests == initial_cache_requests
        assert ota_metrics.cache_external_hits == initial_cache_hits

    @patch("otaclient.metrics.logger")
    def test_shm_merge_preserves_non_shared_fields(self, mock_logger):
        """Test that merge preserves fields not in shared memory data."""
        ota_metrics = metrics.OTAMetricsData()

        # Set some initial values for non-shared fields
        ota_metrics.session_id = "test_session"
        ota_metrics.current_firmware_version = "1.0.0"
        ota_metrics.downloaded_bytes = 1000

        # Create mock shared memory data
        shm_metrics = metrics.OTAMetricsSharedMemoryData()
        shm_metrics.cache_total_requests = 100

        # Test the merge
        ota_metrics.shm_merge(shm_metrics)

        # Verify shared fields were updated
        assert ota_metrics.cache_total_requests == 100

        # Verify non-shared fields were preserved
        assert ota_metrics.session_id == "test_session"
        assert ota_metrics.current_firmware_version == "1.0.0"
        assert ota_metrics.downloaded_bytes == 1000

    @patch("otaclient.metrics.logger")
    def test_publish(self, mock_logger):
        """Test publish method of OTAMetricsData."""
        ota_metrics = metrics.OTAMetricsData()
        test_session_id = "test_session_id"
        ota_metrics.session_id = test_session_id

        ota_metrics.publish()

        # Verify logger was called with JSON representation of data
        mock_logger.info.assert_called()
        log_message = mock_logger.info.call_args[0][0]
        log_extra = mock_logger.info.call_args[1]["extra"]

        # Verify the log message is valid JSON and contains our data
        data_dict = json.loads(log_message)
        assert data_dict["session_id"] == test_session_id
        assert data_dict["ecu_id"] == ecu_info.ecu_id
        assert data_dict["otaclient_version"] == __version__

        # Verify log type is correct
        assert log_extra["log_type"] == LogType.METRICS

        # Verify already_published flag is set
        assert ota_metrics._already_published is True

        # Reset the mock and call publish again
        mock_logger.reset_mock()
        ota_metrics.publish()

        # Verify logger was not called again
        mock_logger.info.assert_not_called()

    def test_publish_multiple_times(self):
        """Test that publishing only happens once."""
        ota_metrics = metrics.OTAMetricsData()

        with patch("otaclient.metrics.logger") as mock_logger:
            ota_metrics.publish()
            assert mock_logger.info.call_count == 2

            # Second publish should not call logger again
            ota_metrics.publish()
            assert mock_logger.info.call_count == 2
