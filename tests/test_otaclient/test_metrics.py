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
import logging
import os
import random
import time
from unittest.mock import patch

from _otaclient_version import __version__

from otaclient import metrics
from otaclient._logging import LogType
from otaclient._types import FailureType
from otaclient.configs.cfg import ecu_info

MODULE = metrics.__name__


class TestOTAMetrics:
    """Test class for OTAMetrics functionality."""

    def test_init(self):
        """Test initialization of OTAMetrics."""
        ota_metrics = metrics.OTAMetrics()
        assert ota_metrics.data.otaclient_version == __version__
        assert ota_metrics.data.ecu_id == ecu_info.ecu_id
        assert ota_metrics._already_published is False

    def test_update(self):
        """Test update method of OTAMetrics."""
        ota_metrics = metrics.OTAMetrics()

        # Test updating valid attributes
        test_timestamp = int(time.time())
        test_session_id = os.urandom(8).hex()
        test_failure_type = random.choice(list(FailureType))
        test_failure_reason = "Test failure reason"
        test_firmware_version = "1.2.3"

        ota_metrics.update(
            download_start_timestamp=test_timestamp,
            session_id=test_session_id,
            failure_type=test_failure_type,
            failure_reason=test_failure_reason,
            target_firmware_version=test_firmware_version,
            downloaded_bytes=1024,
            downloaded_errors=2,
        )

        assert ota_metrics.data.download_start_timestamp == test_timestamp
        assert ota_metrics.data.session_id == test_session_id
        assert ota_metrics.data.failure_type == test_failure_type
        assert ota_metrics.data.failure_reason == test_failure_reason
        assert ota_metrics.data.target_firmware_version == test_firmware_version
        assert ota_metrics.data.downloaded_bytes == 1024
        assert ota_metrics.data.downloaded_errors == 2

    def test_update_invalid_key(self, caplog):
        """Test update method with invalid key."""
        ota_metrics = metrics.OTAMetrics()

        with caplog.at_level(logging.WARNING):
            ota_metrics.update(invalid_key="some value")

        assert "Key invalid_key is not found in metrics_data" in caplog.text

    @patch("otaclient.metrics.logger")
    def test_publish(self, mock_logger):
        """Test publish method of OTAMetrics."""
        ota_metrics = metrics.OTAMetrics()
        test_session_id = "test_session_id"
        ota_metrics.update(session_id=test_session_id)

        ota_metrics.publish()

        # Verify logger was called with JSON representation of data
        mock_logger.info.assert_called_once()
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
        ota_metrics = metrics.OTAMetrics()

        with patch("otaclient.metrics.logger") as mock_logger:
            ota_metrics.publish()
            assert mock_logger.info.call_count == 1

            # Second publish should not call logger again
            ota_metrics.publish()
            assert mock_logger.info.call_count == 1
