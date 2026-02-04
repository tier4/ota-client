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
import threading
import time
from pathlib import Path
from queue import Queue

import pytest
import pytest_mock

from otaclient import errors as ota_errors
from otaclient._status_monitor import StatusReport
from otaclient._types import AbortOTAFlag, AbortThreadLock, CriticalZoneFlag
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


class TestAbortRaceCondition:
    """Test that race conditions between abort and final update phases are handled."""

    @pytest.fixture(autouse=True)
    def setup(self):
        # Create multiprocessing events for the flags
        self.shutdown_requested = mp.Event()
        self.reject_abort_event = mp.Event()
        self.abort_ota_flag = AbortOTAFlag(
            shutdown_requested=self.shutdown_requested,
            reject_abort=self.reject_abort_event,
        )

        # Create locks
        self.critical_zone_lock = mp.Lock()
        self.critical_zone_flag = CriticalZoneFlag(self.critical_zone_lock)
        self.abort_thread_lock = AbortThreadLock()

    def _simulate_servicer_abort(self) -> str:
        """Simulates servicer trying to process abort, returns result."""
        with self.abort_thread_lock.acquire_lock_with_release(blocking=True):
            if self.shutdown_requested.is_set():
                return "already_processed"

            with self.critical_zone_flag.acquire_lock_with_release(blocking=True):
                # Check reject_abort INSIDE the lock (the fix)
                if self.reject_abort_event.is_set():
                    return "rejected_final_phase"

                self.shutdown_requested.set()
                return "abort_processed"

    def test_abort_rejected_when_reject_abort_set(self):
        """Test abort is rejected when reject_abort is set (in final phase)."""
        # Set reject_abort before servicer tries to process
        self.reject_abort_event.set()

        # Servicer tries to abort
        result = self._simulate_servicer_abort()

        assert result == "rejected_final_phase"
        assert not self.shutdown_requested.is_set()

    def test_abort_proceeds_when_not_in_final_phase(self):
        """Test abort proceeds normally when not in final phase."""
        # reject_abort is NOT set (not in final phase)
        assert not self.reject_abort_event.is_set()

        # Servicer tries to abort
        result = self._simulate_servicer_abort()

        assert result == "abort_processed"
        assert self.shutdown_requested.is_set()

    def test_queued_abort_rejected_when_final_phase_entered(self):
        """Test queued abort is rejected if ota-core enters final phase while waiting."""
        abort_result = []
        barrier = threading.Barrier(2)

        def simulate_ota_core():
            """Simulates ota-core holding lock then entering final phase."""
            with self.critical_zone_flag.acquire_lock_with_release(blocking=True):
                # Signal that we're in critical zone
                barrier.wait()
                # Simulate some work in critical zone
                time.sleep(0.1)
            # After releasing lock, immediately set reject_abort (entering final phase)
            self.reject_abort_event.set()

        def simulate_servicer_queued_abort():
            """Simulates servicer's queued abort waiting for lock."""
            # Wait for ota-core to be in critical zone
            barrier.wait()
            with self.abort_thread_lock.acquire_lock_with_release(blocking=True):
                if self.shutdown_requested.is_set():
                    abort_result.append("already_processed")
                    return

                # This will block until ota-core releases the lock
                with self.critical_zone_flag.acquire_lock_with_release(blocking=True):
                    # By now, ota-core has set reject_abort
                    if self.reject_abort_event.is_set():
                        abort_result.append("rejected_final_phase")
                        return

                    self.shutdown_requested.set()
                    abort_result.append("abort_processed")

        # Start ota-core thread
        ota_core_thread = threading.Thread(target=simulate_ota_core)
        # Start servicer thread
        servicer_thread = threading.Thread(target=simulate_servicer_queued_abort)

        ota_core_thread.start()
        servicer_thread.start()

        ota_core_thread.join()
        servicer_thread.join()

        assert len(abort_result) == 1
        assert abort_result[0] == "rejected_final_phase"
        assert not self.shutdown_requested.is_set()
