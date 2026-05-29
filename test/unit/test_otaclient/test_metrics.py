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
from dataclasses import fields

import pytest
from _otaclient_version import __version__
from pytest_mock import MockerFixture

from otaclient import metrics
from otaclient._logging import LogType
from otaclient.configs.cfg import ecu_info

MODULE = metrics.__name__

# Fields the shm snapshot carries; `shm_merge` copies each of these onto the
# OTAMetricsData instance (they all also exist on OTAMetricsData, which is the
# contract that makes the merge effective). Derived from the source dataclass
# so new snapshot fields are exercised automatically.
SHARED_CACHE_FIELDS = [f.name for f in fields(metrics.OTAMetricsSharedMemoryData)]


class _BrokenShm:
    """A snapshot stand-in whose every attribute read raises.

    Simulates the real failure `shm_merge` guards against: a detached or
    corrupt `multiprocessing.shared_memory` segment that throws when the
    backing buffer is dereferenced.
    """

    def __getattr__(self, name: str):
        raise RuntimeError("shared memory unavailable")


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

    # ------------------------------ shm_merge ------------------------------ #

    @pytest.mark.parametrize(
        "field", [pytest.param(name, id=name) for name in SHARED_CACHE_FIELDS]
    )
    def test_shm_merge_copies_each_shared_field(self, mock_logger, field: str):
        """Every field the snapshot carries is merged onto the metrics data."""
        ota_metrics = metrics.OTAMetricsData()

        shm_metrics = metrics.OTAMetricsSharedMemoryData()
        setattr(shm_metrics, field, 100)

        ota_metrics.shm_merge(shm_metrics)

        assert getattr(ota_metrics, field) == 100
        mock_logger.warning.assert_not_called()

    def test_shm_merge_overwrites_even_with_shm_defaults(self, mock_logger):
        """The snapshot is authoritative: a zeroed snapshot overwrites prior counters
        (rather than merging max/only-if-set)."""
        ota_metrics = metrics.OTAMetricsData()
        for field in SHARED_CACHE_FIELDS:
            setattr(ota_metrics, field, 999)

        # a freshly-constructed snapshot is all-zero
        ota_metrics.shm_merge(metrics.OTAMetricsSharedMemoryData())

        for field in SHARED_CACHE_FIELDS:
            assert getattr(ota_metrics, field) == 0

    def test_shm_merge_none_is_noop(self, mock_logger):
        ota_metrics = metrics.OTAMetricsData()
        initial = {f: getattr(ota_metrics, f) for f in SHARED_CACHE_FIELDS}

        ota_metrics.shm_merge(None)

        for field, value in initial.items():
            assert getattr(ota_metrics, field) == value
        mock_logger.warning.assert_not_called()

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

    def test_shm_merge_swallows_exception(self, mock_logger):
        """A failure while reading the snapshot is logged, never propagated."""
        ota_metrics = metrics.OTAMetricsData()

        # must not raise even though every snapshot read throws
        ota_metrics.shm_merge(_BrokenShm())

        mock_logger.warning.assert_called_once()

    # ------------------------------- publish ------------------------------- #

    def test_publish_logs_metrics_payload(self, mock_logger):
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
        # the bookkeeping flag set in __post_init__ must not leak into the payload
        assert "_already_published" not in data_dict

        assert log_extra["log_type"] == LogType.METRICS

        assert ota_metrics._already_published is True

    @pytest.mark.parametrize(
        "n_calls", [pytest.param(2, id="2x"), pytest.param(5, id="5x")]
    )
    def test_publish_is_idempotent(self, mock_logger, n_calls: int):
        """Publishing repeatedly only emits the metrics once."""
        ota_metrics = metrics.OTAMetricsData()

        for _ in range(n_calls):
            ota_metrics.publish()

        mock_logger.info.assert_called_once()
        assert ota_metrics._already_published is True

    def test_publish_failure_is_retryable(self, mock_logger, mocker: MockerFixture):
        """A failed publish is logged via error() and leaves the data un-published,
        so a subsequent call retries instead of silently dropping the metrics."""
        ota_metrics = metrics.OTAMetricsData()
        mocker.patch(
            f"{MODULE}.json.dumps",
            side_effect=[RuntimeError("boom"), "{}"],
        )

        # first attempt fails: error logged, nothing emitted, flag not latched
        ota_metrics.publish()
        mock_logger.error.assert_called_once()
        mock_logger.info.assert_not_called()
        assert ota_metrics._already_published is False

        # second attempt retries and succeeds
        ota_metrics.publish()
        mock_logger.info.assert_called_once()
        assert ota_metrics._already_published is True
