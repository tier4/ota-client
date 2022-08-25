import os
import time
import grpc.aio
import pytest
from multiprocessing import Process
from pathlib import Path
from pytest_mock import MockerFixture
from pytest import LogCaptureFixture

FIRST_LINE_LOG = "d3b6bdb | 2021-10-27 09:36:48 +0900 | Initial commit"


async def _terminate_server(server: grpc.aio.Server):
    await server.stop(None)


class _dummy_OtaClientStub:
    def host_addr(self) -> str:
        return "127.0.0.1"


class TestMain:
    @pytest.fixture(autouse=True)
    def patch_main(self, mocker: MockerFixture, tmp_path: Path):
        mocker.patch("app.ota_client_service.OtaClientStub", _dummy_OtaClientStub)
        mocker.patch(
            "app.ota_client_service.service_wait_for_termination",
            _terminate_server,
        )

        self._sys_exit_mocker = mocker.MagicMock(side_effect=ValueError)
        mocker.patch("app.main.sys.exit", self._sys_exit_mocker)

        version_file = tmp_path / "version.txt"
        version_file.write_text(FIRST_LINE_LOG)
        mocker.patch("app.main.VERSION_FILE", version_file)

    @pytest.fixture
    def background_process(self):
        def _waiting():
            time.sleep(1234)

        _p = Process(target=_waiting)
        try:
            _p.start()
            yield _p.pid
        finally:
            _p.kill()

    def test_main_with_version(self, caplog: LogCaptureFixture):
        from app.main import main

        main()
        assert caplog.records[0].msg == "started"
        assert caplog.records[1].msg == FIRST_LINE_LOG
        assert Path("/var/run/ota-client.lock").read_text() == f"{os.getpid()}"

    def test_with_other_otaclient_started(self, background_process):
        from app.main import main

        _other_pid = f"{background_process}"
        _lock_file = Path("/var/run/ota-client.lock")
        _lock_file.write_text(_other_pid)
        _our_pid = f"{os.getpid()}"

        with pytest.raises(ValueError):
            main()
        self._sys_exit_mocker.assert_called_once_with(
            f"another instance of ota-client(pid: {_other_pid}) is running, abort"
        )
        assert Path("/var/run/ota-client.lock").read_text() != _our_pid
