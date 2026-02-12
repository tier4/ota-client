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
from otaclient._status_monitor import OTAStatusChangeReport, StatusReport
from otaclient._types import AbortState, OTAAbortState, OTAStatus
from otaclient.ota_core import _updater

OTA_UPDATER_MODULE = _updater.__name__


class MockOTAUpdater(_updater.OTAUpdaterBase):
    """Concrete implementation of OTAUpdaterBase for testing."""

    def _process_metadata(self) -> None:
        pass

    def _download_delta_resources(self, delta_digests) -> None:
        pass


class TestOTAUpdaterAbortState:
    """Test that abort state is properly managed in all execution paths."""

    @pytest.fixture(autouse=True)
    def setup(
        self,
        mocker: pytest_mock.MockerFixture,
        tmp_path: Path,
    ):
        # Create a simple queue for status reports
        self.status_report_queue: Queue[StatusReport] = Queue()

        # Create OTAAbortState with a real mp.Value
        self.abort_value = mp.Value("i", AbortState.NONE)
        self.abort_ota_state = OTAAbortState(self.abort_value)

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
            abort_ota_state=self.abort_ota_state,
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

    def test_successful_update_resets_final_phase(
        self, mock_updater: MockOTAUpdater, mocker: pytest_mock.MockerFixture
    ):
        """Test that FINAL_PHASE state is set before post_update and reset in finally."""
        mocker.patch.object(mock_updater, "_process_metadata")
        mocker.patch.object(mock_updater, "_pre_update")
        mocker.patch.object(mock_updater, "_in_update")
        mocker.patch.object(mock_updater, "_post_update")
        mocker.patch.object(mock_updater, "_finalize_update")

        mock_updater.execute()

        # After successful execute, state should have been FINAL_PHASE
        # but reset_final_phase in finally resets it to NONE (on failure path)
        # On success path, _finalize_update reboots so this code doesn't run in production
        # In test, _finalize_update is mocked so reset_final_phase runs
        assert self.abort_ota_state.state == AbortState.NONE

    def test_abort_between_metadata_and_pre_update(
        self, mock_updater: MockOTAUpdater, mocker: pytest_mock.MockerFixture
    ):
        """Test that abort is caught between _process_metadata and _pre_update."""
        mocker.patch.object(mock_updater, "_process_metadata")
        mocker.patch.object(mock_updater, "_pre_update")
        mocker.patch.object(mock_updater, "_in_update")
        mocker.patch.object(mock_updater, "_post_update")
        mocker.patch.object(mock_updater, "_finalize_update")

        # Set abort state to REQUESTED before execute
        self.abort_value.value = AbortState.REQUESTED

        with pytest.raises(ota_errors.OTAAborted):
            mock_updater.execute()

        # Abort should have been processed: REQUESTED → ABORTING → ABORTED
        assert self.abort_ota_state.state == AbortState.ABORTED
        self.mock_boot_controller.on_abort.assert_called_once()
        # _pre_update should NOT have been called
        mock_updater._pre_update.assert_not_called()

    def test_abort_between_pre_update_and_in_update(
        self, mock_updater: MockOTAUpdater, mocker: pytest_mock.MockerFixture
    ):
        """Test that abort is caught between _pre_update and _in_update."""

        def set_abort_requested():
            self.abort_value.value = AbortState.REQUESTED

        mocker.patch.object(mock_updater, "_process_metadata")
        mocker.patch.object(
            mock_updater, "_pre_update", side_effect=set_abort_requested
        )
        mocker.patch.object(mock_updater, "_in_update")
        mocker.patch.object(mock_updater, "_post_update")
        mocker.patch.object(mock_updater, "_finalize_update")

        with pytest.raises(ota_errors.OTAAborted):
            mock_updater.execute()

        assert self.abort_ota_state.state == AbortState.ABORTED
        self.mock_boot_controller.on_abort.assert_called_once()
        mock_updater._in_update.assert_not_called()

    def test_abort_after_in_update_via_enter_final_phase(
        self, mock_updater: MockOTAUpdater, mocker: pytest_mock.MockerFixture
    ):
        """Test that abort requested during _in_update is caught by enter_final_phase."""

        def set_abort_requested():
            self.abort_value.value = AbortState.REQUESTED

        mocker.patch.object(mock_updater, "_process_metadata")
        mocker.patch.object(mock_updater, "_pre_update")
        mocker.patch.object(mock_updater, "_in_update", side_effect=set_abort_requested)
        mocker.patch.object(mock_updater, "_post_update")
        mocker.patch.object(mock_updater, "_finalize_update")

        with pytest.raises(ota_errors.OTAAborted):
            mock_updater.execute()

        assert self.abort_ota_state.state == AbortState.ABORTED
        self.mock_boot_controller.on_abort.assert_called_once()
        mock_updater._post_update.assert_not_called()

    def test_no_abort_when_not_requested(
        self, mock_updater: MockOTAUpdater, mocker: pytest_mock.MockerFixture
    ):
        """Test normal execution when no abort is requested."""
        mocker.patch.object(mock_updater, "_process_metadata")
        mocker.patch.object(mock_updater, "_pre_update")
        mocker.patch.object(mock_updater, "_in_update")
        mocker.patch.object(mock_updater, "_post_update")
        mocker.patch.object(mock_updater, "_finalize_update")

        mock_updater.execute()

        # All phases should have been called
        mock_updater._process_metadata.assert_called_once()
        mock_updater._pre_update.assert_called_once()
        mock_updater._in_update.assert_called_once()
        mock_updater._post_update.assert_called_once()
        mock_updater._finalize_update.assert_called_once()
        # No abort cleanup
        self.mock_boot_controller.on_abort.assert_not_called()

    def test_final_phase_state_on_post_update_error(
        self, mock_updater: MockOTAUpdater, mocker: pytest_mock.MockerFixture
    ):
        """Test that FINAL_PHASE is reset when _post_update raises an exception."""
        mocker.patch.object(mock_updater, "_process_metadata")
        mocker.patch.object(mock_updater, "_pre_update")
        mocker.patch.object(mock_updater, "_in_update")
        mocker.patch.object(
            mock_updater,
            "_post_update",
            side_effect=Exception("Post update failed"),
        )
        mocker.patch.object(mock_updater, "_finalize_update")

        with pytest.raises(ota_errors.ApplyOTAUpdateFailed):
            mock_updater.execute()

        # FINAL_PHASE should be reset to NONE by reset_final_phase() in finally
        assert self.abort_ota_state.state == AbortState.NONE

    def test_ota_error_propagated(
        self, mock_updater: MockOTAUpdater, mocker: pytest_mock.MockerFixture
    ):
        """Test that OTAError is re-raised and on_operation_failure is called."""
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

        with pytest.raises(ota_errors.ApplyOTAUpdateFailed):
            mock_updater.execute()

        self.mock_boot_controller.on_operation_failure.assert_called_once()

    def test_enter_final_phase_closes_abort_window(
        self, mock_updater: MockOTAUpdater, mocker: pytest_mock.MockerFixture
    ):
        """Test that enter_final_phase transitions NONE → FINAL_PHASE."""
        abort_state_during_post_update = []

        def capture_state():
            abort_state_during_post_update.append(self.abort_ota_state.state)

        mocker.patch.object(mock_updater, "_process_metadata")
        mocker.patch.object(mock_updater, "_pre_update")
        mocker.patch.object(mock_updater, "_in_update")
        mocker.patch.object(mock_updater, "_post_update", side_effect=capture_state)
        mocker.patch.object(mock_updater, "_finalize_update")

        mock_updater.execute()

        # During _post_update, abort state should be FINAL_PHASE
        assert len(abort_state_during_post_update) == 1
        assert abort_state_during_post_update[0] == AbortState.FINAL_PHASE

    def test_abort_during_in_update_phase(
        self, mock_updater: MockOTAUpdater, mocker: pytest_mock.MockerFixture
    ):
        """Test that OTAAborted raised during _in_update triggers cleanup."""
        mocker.patch.object(mock_updater, "_process_metadata")
        mocker.patch.object(mock_updater, "_pre_update")
        mocker.patch.object(mock_updater, "_post_update")
        mocker.patch.object(mock_updater, "_finalize_update")

        # Simulate _check_abort() firing inside _in_update (e.g., during download).
        # _check_abort transitions REQUESTED → ABORTING before raising.
        def simulate_intra_phase_abort():
            self.abort_value.value = AbortState.ABORTING
            raise ota_errors.OTAAborted("abort during download", module=__name__)

        mocker.patch.object(
            mock_updater, "_in_update", side_effect=simulate_intra_phase_abort
        )

        with pytest.raises(ota_errors.OTAAborted):
            mock_updater.execute()

        # _do_abort should have been called in the except handler
        assert self.abort_ota_state.state == AbortState.ABORTED
        self.mock_boot_controller.on_abort.assert_called_once()

    def test_aborting_status_report_sent(
        self, mock_updater: MockOTAUpdater, mocker: pytest_mock.MockerFixture
    ):
        """Test that ABORTING OTAStatusChangeReport is sent during abort cleanup."""
        mocker.patch.object(mock_updater, "_process_metadata")
        mocker.patch.object(mock_updater, "_pre_update")
        mocker.patch.object(mock_updater, "_in_update")
        mocker.patch.object(mock_updater, "_post_update")
        mocker.patch.object(mock_updater, "_finalize_update")

        # Set abort state to REQUESTED before execute
        self.abort_value.value = AbortState.REQUESTED

        with pytest.raises(ota_errors.OTAAborted):
            mock_updater.execute()

        # Collect all status reports from the queue
        reports = []
        while not self.status_report_queue.empty():
            reports.append(self.status_report_queue.get_nowait())

        # Verify ABORTING status report was sent
        aborting_reports = [
            r
            for r in reports
            if isinstance(r.payload, OTAStatusChangeReport)
            and r.payload.new_ota_status == OTAStatus.ABORTING
        ]
        assert len(aborting_reports) == 1
