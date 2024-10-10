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

import queue

import pytest

from otaclient._types import OTAStatus
from otaclient.stats_monitor import TERMINATE_SENTINEL, OTAClientStatsCollector

SESSION_ID = "valid_session_id"
INVALID_SESSION_ID = "invalid_session_id"


class TestStatsMonitor:

    @pytest.fixture(scope="class")
    def msg_queue(self):
        return queue.Queue()

    @pytest.fixture(scope="class", autouse=True)
    def stats_monitor(self, msg_queue: queue.Queue):
        t = None
        try:
            _stats_monitor_instance = OTAClientStatsCollector(msg_queue)
            t = _stats_monitor_instance.start()
            yield _stats_monitor_instance
        finally:
            msg_queue.put_nowait(TERMINATE_SENTINEL)
            if t:
                t.join()

    def test_otaclient_startup(self, msg_queue, stats_monitor: OTAClientStatsCollector):
        ota_status = OTAStatus.SUCCESS
