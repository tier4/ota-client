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

import pytest

from otaclient import _logging

MODULE = _logging.__name__
logger = logging.getLogger(__name__)


@pytest.mark.parametrize(
    "test_log_msg, test_extra, expected_log_type",
    [
        ("emit one logging entry", None, _logging.LogType.LOG),
        (
            "emit one logging entry",
            {"log_type": _logging.LogType.LOG},
            _logging.LogType.LOG,
        ),
        (
            "emit one metrics entry",
            {"log_type": _logging.LogType.METRICS},
            _logging.LogType.METRICS,
        ),
    ],
)
def test_server_logger(test_log_msg, test_extra, expected_log_type):
    # ------ setup test ------ #
    _handler = _logging._LogTeeHandler()
    logger.addHandler(_handler)

    # ------ execution ------ #
    logger.info(test_log_msg, extra=test_extra)

    # ------ clenaup ------ #
    logger.removeHandler(_handler)

    # ------ check result ------ #
    _queue = _handler._queue
    _log = _queue.get_nowait()
    assert _log is not None
    assert _log.log_type == expected_log_type
    assert _log.message == test_log_msg
