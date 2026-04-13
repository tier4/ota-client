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

from queue import Queue

import pytest
import pytest_mock

from otaclient._status_monitor import StatusReport
from otaclient._types import AbortRequestV2, AbortState, OTAStatus
from otaclient.ota_core._abort_handler import AbortHandler, WAIT_FOR_STATUS_REPORT


class TestAbortHandlerStatusWait:
    """Test that AbortHandler waits for status report propagation."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.status_report_queue: Queue[StatusReport] = Queue()

    def _make_handler(self, mocker) -> AbortHandler:
        ota_client = mocker.MagicMock()
        ota_client.live_ota_status = OTAStatus.UPDATING
        ota_client.report_status.side_effect = self.status_report_queue.put_nowait
        handler = AbortHandler(
            ota_client=ota_client,
        )
        handler.set_session(session_id="test_session")
        return handler

    def test_perform_abort_waits_for_status_report(self, mocker):
        """Test that _perform_abort sleeps to allow status report propagation."""
        handler = self._make_handler(mocker)
        mocker.patch("otaclient.ota_core._abort_handler.os.kill")
        mock_sleep = mocker.patch("otaclient.ota_core._abort_handler.time.sleep")

        handler._state = AbortState.ABORTING
        handler._perform_abort()

        mock_sleep.assert_called_once_with(WAIT_FOR_STATUS_REPORT)
