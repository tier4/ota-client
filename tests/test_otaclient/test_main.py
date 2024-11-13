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


import os
import time
from multiprocessing import Process
from pathlib import Path

import pytest
from pytest import LogCaptureFixture
from pytest_mock import MockerFixture

from otaclient.configs.cfg import cfg as otaclient_cfg

FIRST_LINE_LOG = "d3b6bdb | 2021-10-27 09:36:48 +0900 | Initial commit"
MAIN_MODULE = "otaclient.main"
UTILS_MODULE = "otaclient.utils"


class TestMain:
    @pytest.fixture(autouse=True)
    def patch_main(self, mocker: MockerFixture, tmp_path: Path):
        mocker.patch(f"{MAIN_MODULE}.launch_otaclient_grpc_server")

        self._sys_exit_mocker = mocker.MagicMock(side_effect=ValueError)
        mocker.patch(f"{UTILS_MODULE}.sys.exit", self._sys_exit_mocker)

    @pytest.fixture
    def background_process(self):
        def _waiting():
            time.sleep(1234)

        _p = Process(target=_waiting)
        try:
            _p.start()
            Path(otaclient_cfg.OTACLIENT_PID_FILE).write_text(f"{_p.pid}")
            yield _p.pid
        finally:
            _p.kill()

    def test_main(self, caplog: LogCaptureFixture):
        from otaclient.main import main

        main()
        assert caplog.records[0].msg == "started"
        assert Path(otaclient_cfg.OTACLIENT_PID_FILE).read_text() == f"{os.getpid()}"

    def test_with_other_otaclient_started(self, background_process):
        from otaclient.main import main

        _other_pid = f"{background_process}"
        with pytest.raises(ValueError):
            main()
        self._sys_exit_mocker.assert_called_once()
        assert Path(otaclient_cfg.OTACLIENT_PID_FILE).read_text() == _other_pid
