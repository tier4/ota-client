import os
import pytest
import grpc
import json
from unittest.mock import ANY


@pytest.fixture
def start_service_with_ota_client_mock(mocker):
    from ota_client_service import (
        OtaClientServiceV2,
        service_start,
        service_stop,
    )
    from ota_client_stub import OtaClientStub
    from ota_client import OtaClient
    import otaclient_v2_pb2 as v2
    import otaclient_v2_pb2_grpc as v2_grpc

    ota_client_mock = mocker.Mock(spec=OtaClient)
    mocker.patch("ota_client_stub.OtaClient", return_value=ota_client_mock)

    ota_client_stub = OtaClientStub()

    ota_client_service_v2 = OtaClientServiceV2(ota_client_stub)

    server = service_start(
        "localhost:50051",
        [
            {"grpc": v2_grpc, "instance": ota_client_service_v2},
        ],
    )

    yield ota_client_mock

    service_stop(server)


def test_ota_client_service_update(mocker, start_service_with_ota_client_mock):
    import otaclient_v2_pb2 as v2
    import otaclient_v2_pb2_grpc as v2_grpc
    from ota_client import OtaClientFailureType

    ota_client_mock = start_service_with_ota_client_mock

    def mock_update(version, url, cookies, event, can_reboot):
        event.set()
        return OtaClientFailureType.NO_FAILURE

    ota_client_mock.update.side_effect = mock_update

    with grpc.insecure_channel("localhost:50051") as channel:
        request = v2.UpdateRequest()
        req_ecu = request.ecu.add()
        req_ecu.ecu_id = "autoware"
        req_ecu.version = "1.2.3.a"
        req_ecu.url = "https://foo.bar.com/ota-data"
        req_ecu.cookies = json.dumps({"test": "my data"})
        service = v2_grpc.OtaClientServiceStub(channel)
        response = service.Update(request)

        response_exp = v2.UpdateResponse()
        res_ecu = response_exp.ecu.add()
        res_ecu.ecu_id = "autoware"
        res_ecu.result = v2.FailureType.NO_FAILURE
        assert response == response_exp

    ota_client_mock.update.assert_called_once_with(
        "1.2.3.a",
        "https://foo.bar.com/ota-data",
        '{"test": "my data"}',
        ANY,
        ANY,
    )


def test_ota_client_service_rollback(mocker, start_service_with_ota_client_mock):
    import otaclient_v2_pb2 as v2
    import otaclient_v2_pb2_grpc as v2_grpc
    from ota_client import OtaClientFailureType

    ota_client_mock = start_service_with_ota_client_mock
    ota_client_mock.rollback.return_value = OtaClientFailureType.NO_FAILURE

    with grpc.insecure_channel("localhost:50051") as channel:
        request = v2.RollbackRequest()
        ecu = request.ecu.add()
        ecu.ecu_id = "autoware"
        service = v2_grpc.OtaClientServiceStub(channel)
        response = service.Rollback(request)

        response_exp = v2.RollbackResponse()
        res_ecu = response_exp.ecu.add()
        res_ecu.ecu_id = "autoware"
        res_ecu.result = v2.FailureType.NO_FAILURE
        assert response == response_exp

    ota_client_mock.rollback.assert_called_once()


def test_ota_client_service_status(mocker, start_service_with_ota_client_mock):
    import otaclient_v2_pb2 as v2
    import otaclient_v2_pb2_grpc as v2_grpc
    from ota_client import OtaClientFailureType

    ota_client_mock = start_service_with_ota_client_mock
    status = {
        "status": "SUCCESS",
        "failure_type": "NO_FAILURE",
        "failure_reason": "",
        "version": "1.2.3",
        "update_progress": {
            "phase": "REGULAR",
            "total_regular_files": 99,
            "regular_files_processed": 10,
            "files_processed_copy": 50,
            "files_processed_link": 9,
            "files_processed_download": 40,
            "file_size_processed_copy": 1000,
            "file_size_processed_link": 100,
            "file_size_processed_download": 1000,
            "elapsed_time_copy": 1230,
            "elapsed_time_link": 120,
            "elapsed_time_download": 9870,
            "errors_download": 10,
        },
    }

    ota_client_mock.status.return_value = OtaClientFailureType.NO_FAILURE, status

    with grpc.insecure_channel("localhost:50051") as channel:
        request = v2.StatusRequest()
        service = v2_grpc.OtaClientServiceStub(channel)
        response = service.Status(request)

        response_exp = v2.StatusResponse()
        res_ecu = response_exp.ecu.add()
        res_ecu.ecu_id = "autoware"
        res_ecu.result = v2.FailureType.NO_FAILURE

        res_ecu.status.status = v2.StatusOta.SUCCESS
        res_ecu.status.failure = v2.FailureType.NO_FAILURE
        res_ecu.status.failure_reason = ""
        res_ecu.status.version = "1.2.3"
        res_ecu.status.progress.phase = v2.StatusProgressPhase.REGULAR
        res_ecu.status.progress.total_regular_files = 99
        res_ecu.status.progress.regular_files_processed = 10

        res_ecu.status.progress.files_processed_copy = 50
        res_ecu.status.progress.files_processed_link = 9
        res_ecu.status.progress.files_processed_download = 40
        res_ecu.status.progress.file_size_processed_copy = 1000
        res_ecu.status.progress.file_size_processed_link = 100
        res_ecu.status.progress.file_size_processed_download = 1000

        res_ecu.status.progress.elapsed_time_copy.FromMilliseconds(1230)
        res_ecu.status.progress.elapsed_time_link.FromMilliseconds(120)
        res_ecu.status.progress.elapsed_time_download.FromMilliseconds(9870)
        res_ecu.status.progress.errors_download = 10

        assert response == response_exp

    ota_client_mock.status.assert_called_once()
