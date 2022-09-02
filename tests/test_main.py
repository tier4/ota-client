import os
import time
import pytest
from multiprocessing import Process
from pathlib import Path
from pytest_mock import MockerFixture
from pytest import LogCaptureFixture

from otaclient.app.configs import OTACLIENT_LOCK_FILE

FIRST_LINE_LOG = "d3b6bdb | 2021-10-27 09:36:48 +0900 | Initial commit"
MAIN_MODULE_PATH = "otaclient.app.main"


class TestMain:
    @pytest.fixture(autouse=True)
    def patch_main(self, mocker: MockerFixture, tmp_path: Path):
        mocker.patch(f"{MAIN_MODULE_PATH}.launch_otaclient_grpc_server")

        self._sys_exit_mocker = mocker.MagicMock(side_effect=ValueError)
        mocker.patch(f"{MAIN_MODULE_PATH}.sys.exit", self._sys_exit_mocker)

        version_file = tmp_path / "version.txt"
        version_file.write_text(FIRST_LINE_LOG)
        mocker.patch(f"{MAIN_MODULE_PATH}.EXTRA_VERSION_FILE", version_file)

    @pytest.fixture
    def background_process(self):
        def _waiting():
            time.sleep(1234)

        _p = Process(target=_waiting)
        try:
            _p.start()
            Path(OTACLIENT_LOCK_FILE).write_text(f"{_p.pid}")
            yield _p.pid
        finally:
            _p.kill()

    def test_main_with_version(self, caplog: LogCaptureFixture):
        from otaclient.app.main import main

        main()
        assert caplog.records[0].msg == "started"
        assert caplog.records[1].msg == FIRST_LINE_LOG
        assert Path(OTACLIENT_LOCK_FILE).read_text() == f"{os.getpid()}"

    def test_with_other_otaclient_started(self, background_process):
        from otaclient.app.main import main

        _other_pid = f"{background_process}"
        with pytest.raises(ValueError):
            main()
        self._sys_exit_mocker.assert_called_once()
        assert Path(OTACLIENT_LOCK_FILE).read_text() == _other_pid
