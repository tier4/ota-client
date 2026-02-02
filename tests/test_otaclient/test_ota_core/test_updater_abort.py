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
"""Tests for OTA updater abort handling."""

from __future__ import annotations

from contextlib import contextmanager
from queue import Queue
from unittest.mock import MagicMock, patch

import pytest

from otaclient import errors as ota_errors
from otaclient._types import AbortOTAFlag, CriticalZoneFlag
from otaclient.metrics import OTAMetricsData
from otaclient.ota_core._updater import OTAUpdaterBase


class ConcreteOTAUpdater(OTAUpdaterBase):
    """Concrete implementation of OTAUpdaterBase for testing."""

    def _process_metadata(self):
        pass

    def _download_delta_resources(self, delta_digests):
        pass


class TestOTAUpdaterAbort:
    """Tests for abort handling in OTAUpdaterBase.execute()."""

    @pytest.fixture
    def mock_abort_ota_flag(self):
        """Create a mock AbortOTAFlag with controllable Events."""
        flag = MagicMock(spec=AbortOTAFlag)
        flag.shutdown_requested = MagicMock()
        flag.shutdown_requested.is_set = MagicMock(return_value=False)
        flag.reject_abort = MagicMock()
        return flag

    @pytest.fixture
    def mock_critical_zone_flag(self):
        """Create a mock CriticalZoneFlag."""
        flag = MagicMock(spec=CriticalZoneFlag)

        @contextmanager
        def mock_acquire(blocking=False):
            yield True

        flag.acquire_lock_with_release = mock_acquire
        return flag

    @pytest.fixture
    def mock_updater(self, mock_abort_ota_flag, mock_critical_zone_flag, tmp_path):
        """Create a mock OTAUpdater for testing abort behavior."""
        boot_controller = MagicMock()
        boot_controller.get_standby_slot_path.return_value = tmp_path / "standby"
        boot_controller.standby_slot_dev = "/dev/test"

        ecu_status_flags = MagicMock()
        ecu_status_flags.any_child_ecu_in_update.is_set.return_value = False

        session_workdir = tmp_path / "session_workdir"
        session_workdir.mkdir(parents=True, exist_ok=True)

        status_report_queue = Queue()

        # Patch all internal methods
        with patch.object(ConcreteOTAUpdater, "_pre_update"), patch.object(
            ConcreteOTAUpdater, "_in_update"
        ), patch.object(ConcreteOTAUpdater, "_post_update"), patch.object(
            ConcreteOTAUpdater, "_finalize_update"
        ), patch(
            "otaclient.ota_core._updater.ensure_umount"
        ), patch(
            "shutil.rmtree"
        ):
            updater = ConcreteOTAUpdater(
                version="test_version",
                raw_url_base="http://test.example.com/",
                session_wd=session_workdir,
                downloader_pool=MagicMock(),
                boot_controller=boot_controller,
                ecu_status_flags=ecu_status_flags,
                critical_zone_flag=mock_critical_zone_flag,
                abort_ota_flag=mock_abort_ota_flag,
                session_id="test_session",
                status_report_queue=status_report_queue,
                metrics=OTAMetricsData(),
                shm_metrics_reader=None,
            )

            yield updater, mock_abort_ota_flag, boot_controller

    def test_abort_after_pre_update_raises_exception(self, mock_updater):
        """Test that abort requested during pre_update phase raises OTAAbortRequested."""
        updater, abort_ota_flag, boot_controller = mock_updater

        # Simulate abort being requested after pre_update completes
        call_count = 0

        def shutdown_requested_side_effect():
            nonlocal call_count
            call_count += 1
            # First call (after pre_update) returns True to simulate abort
            return call_count >= 1

        abort_ota_flag.shutdown_requested.is_set.side_effect = (
            shutdown_requested_side_effect
        )

        # Patch methods for this specific test
        with patch.object(updater, "_process_metadata"), patch.object(
            updater, "_pre_update"
        ), patch.object(updater, "_in_update") as mock_in_update, patch.object(
            updater, "_post_update"
        ), patch.object(
            updater, "_finalize_update"
        ), patch(
            "otaclient.ota_core._updater.ensure_umount"
        ), patch(
            "shutil.rmtree"
        ):
            # Execute should raise OTAAbortRequested
            with pytest.raises(ota_errors.OTAAbortRequested):
                updater.execute()

            # Verify boot controller failure handler was called
            boot_controller.on_operation_failure.assert_called_once()

            # Verify _in_update was NOT called (abort happened before it)
            mock_in_update.assert_not_called()

    def test_abort_after_in_update_raises_exception(self, mock_updater):
        """Test that abort requested during _in_update phase raises OTAAbortRequested."""
        updater, abort_ota_flag, boot_controller = mock_updater

        # Simulate abort being requested after _in_update completes
        call_count = 0

        def shutdown_requested_side_effect():
            nonlocal call_count
            call_count += 1
            # First call (after pre_update) returns False
            # Second call (after _in_update) returns True to simulate abort
            return call_count >= 2

        abort_ota_flag.shutdown_requested.is_set.side_effect = (
            shutdown_requested_side_effect
        )

        # Patch methods for this specific test
        with patch.object(updater, "_process_metadata"), patch.object(
            updater, "_pre_update"
        ), patch.object(updater, "_in_update") as mock_in_update, patch.object(
            updater, "_post_update"
        ) as mock_post_update, patch.object(
            updater, "_finalize_update"
        ), patch(
            "otaclient.ota_core._updater.ensure_umount"
        ), patch(
            "shutil.rmtree"
        ):
            # Execute should raise OTAAbortRequested
            with pytest.raises(ota_errors.OTAAbortRequested):
                updater.execute()

            # Verify boot controller failure handler was called
            boot_controller.on_operation_failure.assert_called_once()

            # Verify _in_update was called but _post_update was NOT
            mock_in_update.assert_called_once()
            mock_post_update.assert_not_called()

            # Verify reject_abort was NOT set (abort happened before that)
            abort_ota_flag.reject_abort.set.assert_not_called()

    def test_no_abort_continues_normally(self, mock_updater):
        """Test that update continues normally when no abort is requested."""
        updater, abort_ota_flag, boot_controller = mock_updater

        # No abort requested
        abort_ota_flag.shutdown_requested.is_set.return_value = False

        # Patch methods for this specific test
        with patch.object(
            updater, "_process_metadata"
        ) as mock_process_metadata, patch.object(
            updater, "_pre_update"
        ) as mock_pre_update, patch.object(
            updater, "_in_update"
        ) as mock_in_update, patch.object(
            updater, "_post_update"
        ) as mock_post_update, patch.object(
            updater, "_finalize_update"
        ) as mock_finalize_update, patch(
            "otaclient.ota_core._updater.ensure_umount"
        ), patch(
            "shutil.rmtree"
        ):
            # Execute should complete without raising
            updater.execute()

            # Verify all phases were called
            mock_process_metadata.assert_called_once()
            mock_pre_update.assert_called_once()
            mock_in_update.assert_called_once()
            mock_post_update.assert_called_once()
            mock_finalize_update.assert_called_once()

            # Verify reject_abort was set before final phases
            abort_ota_flag.reject_abort.set.assert_called_once()

            # Verify on_operation_failure was NOT called
            boot_controller.on_operation_failure.assert_not_called()
