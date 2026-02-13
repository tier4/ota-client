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

from pathlib import Path
from queue import Queue

import pytest
import pytest_mock

from otaclient import errors as ota_errors
from otaclient._status_monitor import OTAStatusChangeReport, StatusReport
from otaclient._types import (
    AbortRequestV2,
    AbortState,
    IPCResEnum,
    IPCResponse,
    OTAStatus,
)
from otaclient.ota_core import _updater
from otaclient.ota_core._main import AbortHandler

OTA_UPDATER_MODULE = _updater.__name__


class MockOTAUpdater(_updater.OTAUpdaterBase):
    """Concrete implementation of OTAUpdaterBase for testing."""

    def _process_metadata(self) -> None:
        pass

    def _download_delta_resources(self, delta_digests) -> None:
        pass


class TestOTAUpdaterWithAbortHandler:
    """Test that updater properly delegates abort to AbortHandler."""

    @pytest.fixture(autouse=True)
    def setup(
        self,
        mocker: pytest_mock.MockerFixture,
        tmp_path: Path,
    ):
        self.status_report_queue: Queue[StatusReport] = Queue()
        self.mock_boot_controller = mocker.MagicMock()
        self.session_workdir = tmp_path / "session_workdir"
        self.session_workdir.mkdir(parents=True, exist_ok=True)

    @pytest.fixture
    def mock_abort_handler(self, mocker: pytest_mock.MockerFixture):
        handler = mocker.MagicMock(spec=AbortHandler)
        return handler

    @pytest.fixture
    def mock_updater(
        self,
        mocker: pytest_mock.MockerFixture,
        tmp_path: Path,
        mock_abort_handler,
    ) -> MockOTAUpdater:
        mocker.patch(
            f"{OTA_UPDATER_MODULE}.OTAUpdateInitializer.__init__", return_value=None
        )

        updater = MockOTAUpdater(
            boot_controller=self.mock_boot_controller,
            abort_handler=mock_abort_handler,
        )

        updater._session_workdir = self.session_workdir
        updater._status_report_queue = self.status_report_queue
        updater.session_id = "test_session_id"
        updater.update_version = "test_version"
        updater.ecu_status_flags = mocker.MagicMock()
        updater._metrics = mocker.MagicMock()
        updater._shm_metrics_reader = None

        return updater

    def test_critical_zone_called_around_pre_update(
        self,
        mock_updater: MockOTAUpdater,
        mock_abort_handler,
        mocker: pytest_mock.MockerFixture,
    ):
        """Test that enter/exit_critical_zone is called around _pre_update."""
        call_order = []

        mock_abort_handler.enter_critical_zone.side_effect = lambda: call_order.append(
            "enter_critical_zone"
        )
        mock_abort_handler.exit_critical_zone.side_effect = lambda: call_order.append(
            "exit_critical_zone"
        )
        mock_abort_handler.enter_final_phase.side_effect = lambda: call_order.append(
            "enter_final_phase"
        )

        mocker.patch.object(mock_updater, "_process_metadata")
        mocker.patch.object(
            mock_updater,
            "_pre_update",
            side_effect=lambda: call_order.append("_pre_update"),
        )
        mocker.patch.object(mock_updater, "_in_update")
        mocker.patch.object(mock_updater, "_post_update")
        mocker.patch.object(mock_updater, "_finalize_update")

        mock_updater.execute()

        assert call_order == [
            "enter_critical_zone",
            "_pre_update",
            "exit_critical_zone",
            "enter_final_phase",
        ]

    def test_final_phase_entered_before_post_update(
        self,
        mock_updater: MockOTAUpdater,
        mock_abort_handler,
        mocker: pytest_mock.MockerFixture,
    ):
        """Test that enter_final_phase is called before _post_update."""
        mocker.patch.object(mock_updater, "_process_metadata")
        mocker.patch.object(mock_updater, "_pre_update")
        mocker.patch.object(mock_updater, "_in_update")
        mocker.patch.object(mock_updater, "_post_update")
        mocker.patch.object(mock_updater, "_finalize_update")

        mock_updater.execute()

        mock_abort_handler.enter_final_phase.assert_called_once()
        mock_updater._post_update.assert_called_once()
        mock_updater._finalize_update.assert_called_once()

    def test_abort_signal_from_enter_critical_zone(
        self,
        mock_updater: MockOTAUpdater,
        mock_abort_handler,
        mocker: pytest_mock.MockerFixture,
    ):
        """Test that OTAAbortSignal from enter_critical_zone propagates correctly."""
        mocker.patch.object(mock_updater, "_process_metadata")
        mocker.patch.object(mock_updater, "_pre_update")
        mocker.patch.object(mock_updater, "_in_update")
        mocker.patch.object(mock_updater, "_post_update")
        mocker.patch.object(mock_updater, "_finalize_update")

        mock_abort_handler.enter_critical_zone.side_effect = ota_errors.OTAAbortSignal(
            "abort in progress", module=__name__
        )

        with pytest.raises(ota_errors.OTAAbortSignal):
            mock_updater.execute()

        mock_updater._pre_update.assert_not_called()
        self.mock_boot_controller.on_operation_failure.assert_not_called()

    def test_abort_signal_from_exit_critical_zone(
        self,
        mock_updater: MockOTAUpdater,
        mock_abort_handler,
        mocker: pytest_mock.MockerFixture,
    ):
        """Test that OTAAbortSignal from exit_critical_zone propagates correctly."""
        mocker.patch.object(mock_updater, "_process_metadata")
        mocker.patch.object(mock_updater, "_pre_update")
        mocker.patch.object(mock_updater, "_in_update")
        mocker.patch.object(mock_updater, "_post_update")
        mocker.patch.object(mock_updater, "_finalize_update")

        mock_abort_handler.exit_critical_zone.side_effect = ota_errors.OTAAbortSignal(
            "queued abort executing", module=__name__
        )

        with pytest.raises(ota_errors.OTAAbortSignal):
            mock_updater.execute()

        mock_updater._pre_update.assert_called_once()
        mock_updater._in_update.assert_not_called()
        self.mock_boot_controller.on_operation_failure.assert_not_called()

    def test_abort_signal_from_enter_final_phase(
        self,
        mock_updater: MockOTAUpdater,
        mock_abort_handler,
        mocker: pytest_mock.MockerFixture,
    ):
        """Test that OTAAbortSignal from enter_final_phase propagates correctly."""
        mocker.patch.object(mock_updater, "_process_metadata")
        mocker.patch.object(mock_updater, "_pre_update")
        mocker.patch.object(mock_updater, "_in_update")
        mocker.patch.object(mock_updater, "_post_update")
        mocker.patch.object(mock_updater, "_finalize_update")

        mock_abort_handler.enter_final_phase.side_effect = ota_errors.OTAAbortSignal(
            "abort in progress", module=__name__
        )

        with pytest.raises(ota_errors.OTAAbortSignal):
            mock_updater.execute()

        mock_updater._post_update.assert_not_called()
        self.mock_boot_controller.on_operation_failure.assert_not_called()

    def test_no_abort_normal_execution(
        self,
        mock_updater: MockOTAUpdater,
        mock_abort_handler,
        mocker: pytest_mock.MockerFixture,
    ):
        """Test normal execution when no abort is requested."""
        mocker.patch.object(mock_updater, "_process_metadata")
        mocker.patch.object(mock_updater, "_pre_update")
        mocker.patch.object(mock_updater, "_in_update")
        mocker.patch.object(mock_updater, "_post_update")
        mocker.patch.object(mock_updater, "_finalize_update")

        mock_updater.execute()

        mock_updater._process_metadata.assert_called_once()
        mock_updater._pre_update.assert_called_once()
        mock_updater._in_update.assert_called_once()
        mock_updater._post_update.assert_called_once()
        mock_updater._finalize_update.assert_called_once()
        self.mock_boot_controller.on_abort.assert_not_called()

    def test_ota_error_propagated(
        self,
        mock_updater: MockOTAUpdater,
        mock_abort_handler,
        mocker: pytest_mock.MockerFixture,
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


class TestAbortHandler:
    """Test the AbortHandler state machine and behavior."""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path):
        self.resp_queue: Queue[IPCResponse] = Queue()
        self.status_report_queue: Queue[StatusReport] = Queue()
        self.session_workdir = tmp_path / "session"
        self.session_workdir.mkdir()

    def _make_handler(self, mocker) -> AbortHandler:
        handler = AbortHandler(resp_queue=self.resp_queue)
        handler.set_session(
            boot_controller=mocker.MagicMock(),
            session_workdir=self.session_workdir,
            status_report_queue=self.status_report_queue,
            session_id="test_session",
        )
        return handler

    def _make_abort_request(self) -> AbortRequestV2:
        return AbortRequestV2(request_id="req_1", session_id="abort_session_1")

    def test_initial_state_is_none(self, mocker):
        handler = self._make_handler(mocker)
        assert handler.state == AbortState.NONE

    def test_enter_critical_zone(self, mocker):
        handler = self._make_handler(mocker)
        handler.enter_critical_zone()
        assert handler.state == AbortState.CRITICAL_ZONE

    def test_exit_critical_zone_normal(self, mocker):
        handler = self._make_handler(mocker)
        handler.enter_critical_zone()
        handler.exit_critical_zone()
        assert handler.state == AbortState.NONE

    def test_enter_final_phase(self, mocker):
        handler = self._make_handler(mocker)
        handler.enter_final_phase()
        assert handler.state == AbortState.FINAL_PHASE

    def test_enter_critical_zone_raises_when_aborting(self, mocker):
        handler = self._make_handler(mocker)
        handler._state = AbortState.ABORTING
        with pytest.raises(ota_errors.OTAAbortSignal):
            handler.enter_critical_zone()

    def test_enter_final_phase_raises_when_aborting(self, mocker):
        handler = self._make_handler(mocker)
        handler._state = AbortState.ABORTING
        with pytest.raises(ota_errors.OTAAbortSignal):
            handler.enter_final_phase()

    def test_abort_handler_accepts_when_state_none(self, mocker):
        """Verify NONE → ABORTING, IPC ACCEPT, _perform_abort called."""
        handler = self._make_handler(mocker)
        # Mock _perform_abort to prevent actual process kill
        mocker.patch.object(handler, "_perform_abort")

        request = self._make_abort_request()
        handler._handle(request)

        assert handler.state == AbortState.ABORTING
        resp = self.resp_queue.get_nowait()
        assert resp.res == IPCResEnum.ACCEPT
        assert resp.session_id == request.session_id
        handler._perform_abort.assert_called_once()

    def test_abort_queued_during_critical_zone(self, mocker):
        """Verify CRITICAL_ZONE → REQUESTED, IPC ACCEPT with queued message."""
        handler = self._make_handler(mocker)
        handler.enter_critical_zone()

        request = self._make_abort_request()
        handler._handle(request)

        assert handler.state == AbortState.REQUESTED
        resp = self.resp_queue.get_nowait()
        assert resp.res == IPCResEnum.ACCEPT
        assert "queued" in resp.msg.lower()

    def test_exit_critical_zone_triggers_abort_on_handler_thread(self, mocker):
        """Queue abort during critical zone, call exit_critical_zone,
        verify abort handler thread detects ABORTING and runs cleanup."""
        handler = self._make_handler(mocker)
        mocker.patch.object(handler, "_perform_abort")
        handler.start()

        handler.enter_critical_zone()

        # Submit abort request — handler sets REQUESTED and returns
        request = self._make_abort_request()
        handler.submit(request)

        import time

        time.sleep(0.05)  # let handler thread process request
        assert handler.state == AbortState.REQUESTED

        # exit_critical_zone transitions REQUESTED → ABORTING
        with pytest.raises(ota_errors.OTAAbortSignal):
            handler.exit_critical_zone()

        time.sleep(1.5)  # let handler thread detect ABORTING (polls every 1s)
        handler._perform_abort.assert_called_once()

    def test_abort_handler_rejects_during_final_phase(self, mocker):
        """Verify abort is rejected during FINAL_PHASE."""
        handler = self._make_handler(mocker)
        handler.enter_final_phase()

        request = self._make_abort_request()
        handler._handle(request)

        assert handler.state == AbortState.FINAL_PHASE
        resp = self.resp_queue.get_nowait()
        assert resp.res == IPCResEnum.REJECT_ABORT

    def test_idempotent_abort_when_already_requested(self, mocker):
        """Verify idempotent ACCEPT when abort already in progress."""
        handler = self._make_handler(mocker)
        handler.enter_critical_zone()

        # First abort → queued
        request1 = self._make_abort_request()
        handler._handle(request1)
        assert handler.state == AbortState.REQUESTED

        # Second abort → idempotent accept
        request2 = AbortRequestV2(request_id="req_2", session_id="abort_session_2")
        handler._handle(request2)
        assert handler.state == AbortState.REQUESTED

        # Drain first response
        self.resp_queue.get_nowait()
        resp2 = self.resp_queue.get_nowait()
        assert resp2.res == IPCResEnum.ACCEPT
        assert "already" in resp2.msg.lower()

    def test_idempotent_abort_when_aborting(self, mocker):
        """Verify idempotent ACCEPT when state is ABORTING."""
        handler = self._make_handler(mocker)
        handler._state = AbortState.ABORTING

        request = self._make_abort_request()
        handler._handle(request)

        resp = self.resp_queue.get_nowait()
        assert resp.res == IPCResEnum.ACCEPT
        assert "already" in resp.msg.lower()

    def test_perform_abort_sends_aborting_status(self, mocker):
        """Test that _perform_abort sends ABORTING status report."""
        handler = self._make_handler(mocker)
        # Mock os.kill to prevent process kill
        mocker.patch("otaclient.ota_core._main.os.kill")
        mocker.patch("otaclient.ota_core._main.ensure_umount")
        mocker.patch("otaclient.ota_core._main.shutil.rmtree")

        handler._state = AbortState.ABORTING
        handler._perform_abort()

        assert handler.state == AbortState.ABORTED
        handler._boot_controller.on_abort.assert_called_once()

        # Check ABORTING status was reported
        reports = []
        while not self.status_report_queue.empty():
            reports.append(self.status_report_queue.get_nowait())

        aborting_reports = [
            r
            for r in reports
            if isinstance(r.payload, OTAStatusChangeReport)
            and r.payload.new_ota_status == OTAStatus.ABORTING
        ]
        assert len(aborting_reports) == 1
