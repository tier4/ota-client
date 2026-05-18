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
"""Unit tests for `otaclient.metrics`.

Scope is `OTAMetricsData` only: every collaborator (the module-level
`logger`, the `OTAMetricsSharedMemoryData` snapshot dataclass) is either
mocked or constructed in-memory.
"""

from __future__ import annotations

import json

import pytest
from _otaclient_version import __version__
from pytest_mock import MockerFixture

from otaclient import metrics
from otaclient._logging import LogType
from otaclient.configs.cfg import ecu_info

MODULE = metrics.__name__


@pytest.fixture
def mock_logger(mocker: MockerFixture):
    return mocker.patch(f"{MODULE}.logger")


class TestOTAMetricsData:
    """Test class for OTAMetricsData functionality."""

    def test_init(self):
        ota_metrics = metrics.OTAMetricsData()
        assert ota_metrics.otaclient_version == __version__
        assert ota_metrics.ecu_id == ecu_info.ecu_id
        assert ota_metrics._already_published is False

    def test_shm_merge_success(self, mock_logger):
        ota_metrics = metrics.OTAMetricsData()

        shm_metrics = metrics.OTAMetricsSharedMemoryData()
        shm_metrics.cache_total_requests = 100
        shm_metrics.cache_cdn_hits = 20
        shm_metrics.cache_external_hits = 50
        shm_metrics.cache_local_hits = 30

        ota_metrics.shm_merge(shm_metrics)

        assert ota_metrics.cache_total_requests == 100
        assert ota_metrics.cache_cdn_hits == 20
        assert ota_metrics.cache_external_hits == 50
        assert ota_metrics.cache_local_hits == 30

    def test_shm_merge_no_data(self, mock_logger):
        ota_metrics = metrics.OTAMetricsData()

        initial_cache_requests = ota_metrics.cache_total_requests
        initial_cache_hits = ota_metrics.cache_external_hits

        ota_metrics.shm_merge(None)

        assert ota_metrics.cache_total_requests == initial_cache_requests
        assert ota_metrics.cache_external_hits == initial_cache_hits

    def test_shm_merge_preserves_non_shared_fields(self, mock_logger):
        ota_metrics = metrics.OTAMetricsData()

        ota_metrics.session_id = "test_session"
        ota_metrics.current_firmware_version = "1.0.0"
        ota_metrics.downloaded_bytes = 1000

        shm_metrics = metrics.OTAMetricsSharedMemoryData()
        shm_metrics.cache_total_requests = 100

        ota_metrics.shm_merge(shm_metrics)

        assert ota_metrics.cache_total_requests == 100

        assert ota_metrics.session_id == "test_session"
        assert ota_metrics.current_firmware_version == "1.0.0"
        assert ota_metrics.downloaded_bytes == 1000

    def test_publish(self, mock_logger):
        ota_metrics = metrics.OTAMetricsData()
        test_session_id = "test_session_id"
        ota_metrics.session_id = test_session_id

        ota_metrics.publish()

        mock_logger.info.assert_called_once()
        log_message = mock_logger.info.call_args[0][0]
        log_extra = mock_logger.info.call_args[1]["extra"]

        data_dict = json.loads(log_message)
        assert data_dict["session_id"] == test_session_id
        assert data_dict["ecu_id"] == ecu_info.ecu_id
        assert data_dict["otaclient_version"] == __version__

        assert log_extra["log_type"] == LogType.METRICS

        assert ota_metrics._already_published is True

        mock_logger.reset_mock()
        ota_metrics.publish()

        mock_logger.info.assert_not_called()

    def test_publish_multiple_times(self, mock_logger):
        ota_metrics = metrics.OTAMetricsData()

        ota_metrics.publish()
        assert mock_logger.info.call_count == 1

        ota_metrics.publish()
        assert mock_logger.info.call_count == 1
