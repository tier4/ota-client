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

import pytest

from otaclient.ota_core._abort_handler import (
    EXIT_CODE_OTA_ABORTED,
    _abort_signal_handler,
)
from otaclient_common import downloader as downloader_module


@pytest.fixture(autouse=True)
def _not_cancelled():
    downloader_module._downloads_cancelled.clear()
    yield
    downloader_module._downloads_cancelled.clear()


def test_the_abort_signal_stops_the_downloads_before_the_core_exits() -> None:
    """The core exits once its threads have: its downloads in flight are told to stop
    rather than left to finish their files."""
    with pytest.raises(SystemExit) as _exit:
        _abort_signal_handler(10, None)

    assert _exit.value.code == EXIT_CODE_OTA_ABORTED
    assert downloader_module._downloads_cancelled.is_set()
