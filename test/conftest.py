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
from typing import Any, Generator

import pytest
import pytest_mock

from otaclient._status_monitor import (
    TERMINATE_SENTINEL,
    OTAClientStatusCollector,
    StatusReport,
)

MAX_TRACEBACK_SIZE = 2048


@pytest.fixture(scope="class")
def ota_status_collector(
    class_mocker: pytest_mock.MockerFixture,
) -> Generator[tuple[OTAClientStatusCollector, Queue[StatusReport]], Any, None]:
    """Class-scoped status collector with an in-process report queue.

    Used by `_status_monitor` and OTAClient IPC tests that need to push
    status reports through a real `OTAClientStatusCollector` thread.
    """
    _shm_mock = class_mocker.MagicMock()

    _report_queue: Queue[StatusReport] = Queue()
    _status_collector = OTAClientStatusCollector(
        msg_queue=_report_queue,
        shm_status=_shm_mock,
        max_traceback_size=MAX_TRACEBACK_SIZE,
    )
    _collector_thread = _status_collector.start()

    try:
        yield _status_collector, _report_queue
    finally:
        _report_queue.put_nowait(TERMINATE_SENTINEL)
        _collector_thread.join()
