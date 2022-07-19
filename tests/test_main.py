import grpc.aio
import pytest
from pathlib import Path
from pytest_mock import MockerFixture
from pytest import LogCaptureFixture

FIRST_LINE_LOG = "d3b6bdb | 2021-10-27 09:36:48 +0900 | Initial commit"


async def _terminate_server(server: grpc.aio.Server):
    await server.stop(None)


class _dummy_OtaClientStub:
    def host_addr(self) -> str:
        return "127.0.0.1"


@pytest.fixture
def patch_main(mocker: MockerFixture, tmp_path: Path):
    mocker.patch("app.ota_client_service.OtaClientStub", _dummy_OtaClientStub)
    mocker.patch("app.main._check_other_otaclient")
    mocker.patch(
        "app.ota_client_service.service_wait_for_termination",
        _terminate_server,
    )

    version_file = tmp_path / "version.txt"
    version_file.write_text(FIRST_LINE_LOG)
    mocker.patch("app.main.VERSION_FILE", version_file)


def test_main_with_version(
    patch_main,
    caplog: LogCaptureFixture,
):
    from app.main import main

    main()
    assert caplog.records[0].msg == "started"
    assert caplog.records[1].msg == FIRST_LINE_LOG
