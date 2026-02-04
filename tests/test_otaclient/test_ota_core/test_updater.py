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

import multiprocessing as mp
from pathlib import Path
from queue import Queue

import pytest
import pytest_mock

from otaclient import errors as ota_errors
from otaclient._status_monitor import StatusReport
from otaclient._types import AbortOTAFlag, CriticalZoneFlag
from otaclient.ota_core import _updater

OTA_UPDATER_MODULE = _updater.__name__


class MockOTAUpdater(_updater.OTAUpdaterBase):
    """Concrete implementation of OTAUpdaterBase for testing."""

    def _process_metadata(self) -> None:
        pass

    def _download_delta_resources(self, delta_digests) -> None:
        pass


class TestOTAUpdaterRejectAbortFlag:
    """Test that reject_abort flag is properly cleared in all execution paths."""

    @pytest.fixture(autouse=True)
    def setup(
        self,
        mocker: pytest_mock.MockerFixture,
        tmp_path: Path,
    ):
        # Create a simple queue for status reports
        self.status_report_queue: Queue[StatusReport] = Queue()

        # Create multiprocessing events for the flags
        self.shutdown_requested = mp.Event()
        self.reject_abort_event = mp.Event()
        self.abort_ota_flag = AbortOTAFlag(
            shutdown_requested=self.shutdown_requested,
            reject_abort=self.reject_abort_event,
        )

        # Create a lock for critical zone
        self.critical_zone_lock = mp.Lock()
        self.critical_zone_flag = CriticalZoneFlag(self.critical_zone_lock)

        # Mock boot controller
        self.mock_boot_controller = mocker.MagicMock()

        # Session work directory
        self.session_workdir = tmp_path / "session_workdir"
        self.session_workdir.mkdir(parents=True, exist_ok=True)

    @pytest.fixture
    def mock_updater(
        self, mocker: pytest_mock.MockerFixture, tmp_path: Path
    ) -> MockOTAUpdater:
        """Create a mock OTAUpdater instance for testing."""
        # Mock the OTAUpdateInitializer __init__ to avoid complex setup
        mocker.patch(
            f"{OTA_UPDATER_MODULE}.OTAUpdateInitializer.__init__", return_value=None
        )

        updater = MockOTAUpdater(
            boot_controller=self.mock_boot_controller,
            critical_zone_flag=self.critical_zone_flag,
            abort_ota_flag=self.abort_ota_flag,
        )

        # Set required attributes that would normally be set by OTAUpdateInitializer
        updater._session_workdir = self.session_workdir
        updater._status_report_queue = self.status_report_queue
        updater.session_id = "test_session_id"
        updater.update_version = "test_version"
        updater.ecu_status_flags = mocker.MagicMock()
        updater._metrics = mocker.MagicMock()
        updater._shm_metrics_reader = None

        return updater

    def test_reject_abort_cleared_on_success(
        self, mock_updater: MockOTAUpdater, mocker: pytest_mock.MockerFixture
    ):
        """Test that reject_abort is cleared after successful update."""
        # Mock all the update phase methods
        mocker.patch.object(mock_updater, "_process_metadata")
        mocker.patch.object(mock_updater, "_pre_update")
        mocker.patch.object(mock_updater, "_in_update")
        mocker.patch.object(mock_updater, "_post_update")
        mocker.patch.object(mock_updater, "_finalize_update")

        # Verify reject_abort is not set initially
        assert not self.reject_abort_event.is_set()

        # Execute the update
        mock_updater.execute()

        # Verify reject_abort was cleared after execution
        assert not self.reject_abort_event.is_set()

    def test_reject_abort_cleared_on_post_update_error(
        self, mock_updater: MockOTAUpdater, mocker: pytest_mock.MockerFixture
    ):
        """Test that reject_abort is cleared when _post_update raises an exception."""
        # Mock all the update phase methods
        mocker.patch.object(mock_updater, "_process_metadata")
        mocker.patch.object(mock_updater, "_pre_update")
        mocker.patch.object(mock_updater, "_in_update")
        mocker.patch.object(
            mock_updater,
            "_post_update",
            side_effect=Exception("Post update failed"),
        )
        mocker.patch.object(mock_updater, "_finalize_update")

        # Verify reject_abort is not set initially
        assert not self.reject_abort_event.is_set()

        # Execute should raise the exception
        with pytest.raises(ota_errors.ApplyOTAUpdateFailed):
            mock_updater.execute()

        # Verify reject_abort was cleared despite the error
        assert not self.reject_abort_event.is_set()

    def test_reject_abort_cleared_on_finalize_update_error(
        self, mock_updater: MockOTAUpdater, mocker: pytest_mock.MockerFixture
    ):
        """Test that reject_abort is cleared when _finalize_update raises an exception."""
        # Mock all the update phase methods
        mocker.patch.object(mock_updater, "_process_metadata")
        mocker.patch.object(mock_updater, "_pre_update")
        mocker.patch.object(mock_updater, "_in_update")
        mocker.patch.object(mock_updater, "_post_update")
        mocker.patch.object(
            mock_updater,
            "_finalize_update",
            side_effect=Exception("Finalize update failed"),
        )

        # Verify reject_abort is not set initially
        assert not self.reject_abort_event.is_set()

        # Execute should raise the exception
        with pytest.raises(ota_errors.ApplyOTAUpdateFailed):
            mock_updater.execute()

        # Verify reject_abort was cleared despite the error
        assert not self.reject_abort_event.is_set()

    def test_reject_abort_cleared_on_ota_error(
        self, mock_updater: MockOTAUpdater, mocker: pytest_mock.MockerFixture
    ):
        """Test that reject_abort is cleared when an OTAError is raised."""
        # Mock all the update phase methods
        mocker.patch.object(mock_updater, "_process_metadata")
        mocker.patch.object(mock_updater, "_pre_update")
        mocker.patch.object(mock_updater, "_in_update")
        mocker.patch.object(
            mock_updater,
            "_post_update",
            side_effect=ota_errors.ApplyOTAUpdateFailed(
                "OTA error in post update", module=__name__
            ),
        )

        # Verify reject_abort is not set initially
        assert not self.reject_abort_event.is_set()

        # Execute should raise the OTA error
        with pytest.raises(ota_errors.ApplyOTAUpdateFailed):
            mock_updater.execute()

        # Verify reject_abort was cleared despite the error
        assert not self.reject_abort_event.is_set()

    def test_reject_abort_set_before_post_update(
        self, mock_updater: MockOTAUpdater, mocker: pytest_mock.MockerFixture
    ):
        """Test that reject_abort is set before _post_update is called."""
        reject_abort_state_during_post_update = []

        def capture_reject_abort_state():
            # Capture the state of reject_abort when _post_update is called
            reject_abort_state_during_post_update.append(
                self.reject_abort_event.is_set()
            )

        # Mock all the update phase methods
        mocker.patch.object(mock_updater, "_process_metadata")
        mocker.patch.object(mock_updater, "_pre_update")
        mocker.patch.object(mock_updater, "_in_update")
        mocker.patch.object(
            mock_updater, "_post_update", side_effect=capture_reject_abort_state
        )
        mocker.patch.object(mock_updater, "_finalize_update")

        # Execute the update
        mock_updater.execute()

        # Verify reject_abort was set when _post_update was called
        assert len(reject_abort_state_during_post_update) == 1
        assert reject_abort_state_during_post_update[0] is True

        # Verify reject_abort was cleared after execution
        assert not self.reject_abort_event.is_set()


class TestMainProcessAbortHandling:
    """Test main process abort handling logic."""

    @pytest.fixture(autouse=True)
    def setup(self):
        # Create multiprocessing events for the flags
        self.shutdown_requested = mp.Event()
        self.reject_abort_event = mp.Event()
        self.abort_ota_flag = AbortOTAFlag(
            shutdown_requested=self.shutdown_requested,
            reject_abort=self.reject_abort_event,
        )

    def _simulate_main_process_health_check(self) -> bool:
        """Simulate the main process health check logic.

        Returns True if shutdown should proceed, False if abort was rejected.
        """
        if self.shutdown_requested.is_set():
            if self.reject_abort_event.is_set():
                # Reject abort - OTA is in final phase
                self.shutdown_requested.clear()
                return False
            return True
        return False

    def test_abort_rejected_when_in_final_phase(self):
        """Test that abort is rejected when reject_abort is set."""
        # Simulate: abort was requested
        self.shutdown_requested.set()
        # Simulate: ota-core is in final phase
        self.reject_abort_event.set()

        # Main process should reject the abort
        should_shutdown = self._simulate_main_process_health_check()

        assert should_shutdown is False
        assert not self.shutdown_requested.is_set()  # Cleared
        assert self.reject_abort_event.is_set()  # Unchanged

    def test_abort_allowed_when_not_in_final_phase(self):
        """Test that abort is allowed when reject_abort is NOT set."""
        # Simulate: abort was requested
        self.shutdown_requested.set()
        # Simulate: ota-core is NOT in final phase
        assert not self.reject_abort_event.is_set()

        # Main process should allow the abort
        should_shutdown = self._simulate_main_process_health_check()

        assert should_shutdown is True
        assert self.shutdown_requested.is_set()  # Not cleared

    def test_no_action_when_no_abort_requested(self):
        """Test that no action is taken when no abort was requested."""
        # No abort requested
        assert not self.shutdown_requested.is_set()

        should_shutdown = self._simulate_main_process_health_check()

        assert should_shutdown is False
