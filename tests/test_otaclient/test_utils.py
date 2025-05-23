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

import logging
import time

import pytest

from otaclient._utils import wait_and_log

logger = logging.getLogger(__name__)


class _TickingFlag:

    def __init__(self, trigger_in: int) -> None:
        self._trigger_time = time.time() + trigger_in

    def is_set(self) -> bool:
        _now = time.time()
        return _now > self._trigger_time


def test_wait_and_log(caplog: pytest.LogCaptureFixture):
    # NOTE: allow 2 more seconds for expected_trigger_time
    trigger_in, expected_trigger_time = 11, time.time() + 11 + 2
    _flag = _TickingFlag(trigger_in=trigger_in)
    _msg = "ticking flag"

    result = wait_and_log(
        _flag.is_set,
        _msg,
        check_for=True,
        check_interval=1,
        log_interval=2,
        log_func=logger.warning,
    )

    assert result is True
    assert len(caplog.records) == 5
    assert caplog.records[0].levelno == logging.WARNING
    assert caplog.records[0].msg == f"wait for {_msg}: 2s passed ..."
    assert time.time() < expected_trigger_time


def test_wait_and_log_timeout(caplog: pytest.LogCaptureFixture):
    # Create a flag that will never be set (trigger time is very far in the future)
    _flag = _TickingFlag(trigger_in=3600)  # 1 hour in the future
    _msg = "condition that will timeout"
    timeout = 5  # timeout after 5 seconds

    # Test that wait_and_log returns False when it times out
    result = wait_and_log(
        _flag.is_set,
        _msg,
        check_for=True,
        check_interval=1,
        log_interval=2,
        log_func=logger.warning,
        timeout=timeout,
    )

    # Verify the function returned False (condition wasn't met)
    assert result is False
