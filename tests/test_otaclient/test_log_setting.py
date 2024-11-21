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

from otaclient import _logging

MODULE = _logging.__name__
logger = logging.getLogger(__name__)


def test_server_logger():
    test_log_msg = "emit one logging entry"

    # ------ setup test ------ #
    _handler = _logging._LogTeeHandler()
    logger.addHandler(_handler)

    # ------ execution ------ #
    logger.info(test_log_msg)

    # ------ clenaup ------ #
    logger.removeHandler(_handler)

    # ------ check result ------ #
    _queue = _handler._queue
    _log = _queue.get_nowait()
    assert _log == test_log_msg
