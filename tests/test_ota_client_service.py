import pytest
import grpc
import json
from unittest.mock import ANY

from pytest_mock import MockerFixture


@pytest.fixture
def mocked_update():
    from ota_client import OtaClientFailureType, OtaStateSync

    def _update(version, url, cookies, fsm: OtaStateSync):
        """simulate the state changes in ota_client update"""
        with fsm.proceed(fsm._P2, expect=fsm._START) as next_state:
            assert next_state == fsm._S1
        with fsm.proceed(fsm._P2, expect=fsm._S1) as next_state:
            assert next_state == fsm._S2

        assert fsm.wait_on(fsm._END)
        return OtaClientFailureType.NO_FAILURE

    return _update


@pytest.fixture
def start_service_with_ota_client_mock(mocker: MockerFixture, proxy_cfg, mocked_update):
    from ota_client_service import (
        OtaClientServiceV2,
        service_start,
        service_stop,
    )
    from ota_client_stub import OtaClientStub
    from ota_client import OtaClient
    import otaclient_v2_pb2_grpc as v2_grpc

    ota_client_mock = mocker.Mock(spec=OtaClient)
    ota_client_mock.update.side_effect = mocked_update

    mocker.patch("ota_client_stub.OtaClient", return_value=ota_client_mock)
    mocker.patch("ota_client_stub.proxy_cfg", proxy_cfg)

    ota_client_stub = OtaClientStub()
    mocker.patch.object(ota_client_stub, "_ensure_subecu_status")

    ota_client_service_v2 = OtaClientServiceV2(ota_client_stub)

    server = service_start(
        "localhost:50051",
        [
            {"grpc": v2_grpc, "instance": ota_client_service_v2},
        ],
    )

    yield ota_client_mock, ota_client_stub

    service_stop(server)


def test_ota_client_service_update(mocker, start_service_with_ota_client_mock):
    import otaclient_v2_pb2 as v2
    import otaclient_v2_pb2_grpc as v2_grpc
    from ota_client import OtaClientFailureType

    ota_client_mock, _ = start_service_with_ota_client_mock

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
        "1.2.3.a", "https://foo.bar.com/ota-data", '{"test": "my data"}', fsm=ANY
    )


def test_ota_client_service_rollback(mocker, start_service_with_ota_client_mock):
    import otaclient_v2_pb2 as v2
    import otaclient_v2_pb2_grpc as v2_grpc
    from ota_client import OtaClientFailureType

    ota_client_mock, _ = start_service_with_ota_client_mock
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

    ota_client_mock, _ = start_service_with_ota_client_mock
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
            "total_regular_file_size": 987654321,
            "total_elapsed_time": 123456789,
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
        res_ecu.status.progress.total_regular_file_size = 987654321
        res_ecu.status.progress.total_elapsed_time.FromMilliseconds(123456789)

        response_exp.available_ecu_ids.extend(["autoware"])  # default available_ecu_ids

        assert response == response_exp

    ota_client_mock.status.assert_called_once()


def test_ota_client_service_update_with_secondary(
    mocker, start_service_with_ota_client_mock
):
    import otaclient_v2_pb2 as v2
    import otaclient_v2_pb2_grpc as v2_grpc
    from ota_client import OtaClientFailureType

    ota_client_mock, ota_client_stub = start_service_with_ota_client_mock

    mocker.patch.object(
        ota_client_stub._ecu_info,
        "get_secondary_ecus",
        return_value=[{"ecu_id": "subecu", "ip_addr": "192.168.1.2"}],
    )

    sub = v2.UpdateResponse()
    sub_ecu = sub.ecu.add()
    sub_ecu.ecu_id = "subecu"
    sub_ecu.result = v2.FailureType.NO_FAILURE
    mocker.patch.object(ota_client_stub._ota_client_call, "update", return_value=sub)

    with grpc.insecure_channel("localhost:50051") as channel:
        request = v2.UpdateRequest()
        # "autoware" ecu
        req_ecu = request.ecu.add()
        req_ecu.ecu_id = "autoware"
        req_ecu.version = "1.2.3.a"
        req_ecu.url = "https://foo.bar.com/ota-data"
        req_ecu.cookies = json.dumps({"test": "my data"})
        # "subecu" ecu
        req_ecu = request.ecu.add()
        req_ecu.ecu_id = "subecu"
        req_ecu.version = "4.5.6.a"
        req_ecu.url = "https://foo.bar.com/ota-data-subecu"
        req_ecu.cookies = json.dumps({"test": "subecu data"})

        service = v2_grpc.OtaClientServiceStub(channel)
        response = service.Update(request)

        response_exp = v2.UpdateResponse()
        # "autoware" ecu
        res_ecu = response_exp.ecu.add()
        res_ecu.ecu_id = "autoware"
        res_ecu.result = v2.FailureType.NO_FAILURE
        # "subecu" ecu
        res_ecu = response_exp.ecu.add()
        res_ecu.ecu_id = "subecu"
        res_ecu.result = v2.FailureType.NO_FAILURE
        assert response == response_exp

    ota_client_mock.update.assert_called_once_with(
        "1.2.3.a", "https://foo.bar.com/ota-data", '{"test": "my data"}', fsm=ANY
    )


def test_ota_client_service_rollback_with_secondary(
    mocker, start_service_with_ota_client_mock
):
    import otaclient_v2_pb2 as v2
    import otaclient_v2_pb2_grpc as v2_grpc
    from ota_client import OtaClientFailureType

    ota_client_mock, ota_client_stub = start_service_with_ota_client_mock
    ota_client_mock.rollback.return_value = OtaClientFailureType.NO_FAILURE

    mocker.patch.object(
        ota_client_stub._ecu_info,
        "get_secondary_ecus",
        return_value=[{"ecu_id": "subecu", "ip_addr": "192.168.1.2"}],
    )

    sub = v2.RollbackResponse()
    sub_ecu = sub.ecu.add()
    sub_ecu.ecu_id = "subecu"
    sub_ecu.result = v2.FailureType.NO_FAILURE
    mocker.patch.object(ota_client_stub._ota_client_call, "rollback", return_value=sub)

    with grpc.insecure_channel("localhost:50051") as channel:
        request = v2.RollbackRequest()
        # "autoware" ecu
        req_ecu = request.ecu.add()
        req_ecu.ecu_id = "autoware"
        # "subecu" ecu
        req_ecu = request.ecu.add()
        req_ecu.ecu_id = "subecu"

        service = v2_grpc.OtaClientServiceStub(channel)
        response = service.Rollback(request)

        response_exp = v2.RollbackResponse()
        # "subecu" ecu
        res_ecu = response_exp.ecu.add()
        res_ecu.ecu_id = "subecu"
        res_ecu.result = v2.FailureType.NO_FAILURE
        # "autoware" ecu
        res_ecu = response_exp.ecu.add()
        res_ecu.ecu_id = "autoware"
        res_ecu.result = v2.FailureType.NO_FAILURE
        assert response == response_exp

    ota_client_mock.rollback.assert_called_once()


def test_ota_client_service_status_with_secondary(
    mocker, start_service_with_ota_client_mock
):
    import otaclient_v2_pb2 as v2
    import otaclient_v2_pb2_grpc as v2_grpc
    from ota_client import OtaClientFailureType

    ota_client_mock, ota_client_stub = start_service_with_ota_client_mock
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
    mocker.patch.object(
        ota_client_stub._ecu_info,
        "get_secondary_ecus",
        return_value=[{"ecu_id": "subecu", "ip_addr": "192.168.1.2"}],
    )
    sub = v2.StatusResponse()
    sub_ecu = sub.ecu.add()
    sub_ecu.ecu_id = "subecu"
    sub_ecu.result = v2.FailureType.NO_FAILURE

    sub_ecu.status.status = v2.StatusOta.SUCCESS
    sub_ecu.status.failure = v2.FailureType.NO_FAILURE
    sub_ecu.status.failure_reason = ""
    sub_ecu.status.version = "4.5.6"
    sub_ecu.status.progress.phase = v2.StatusProgressPhase.REGULAR
    sub_ecu.status.progress.total_regular_files = 999
    sub_ecu.status.progress.regular_files_processed = 109

    sub_ecu.status.progress.files_processed_copy = 509
    sub_ecu.status.progress.files_processed_link = 99
    sub_ecu.status.progress.files_processed_download = 409
    sub_ecu.status.progress.file_size_processed_copy = 10009
    sub_ecu.status.progress.file_size_processed_link = 1009
    sub_ecu.status.progress.file_size_processed_download = 10009

    sub_ecu.status.progress.elapsed_time_copy.FromMilliseconds(12309)
    sub_ecu.status.progress.elapsed_time_link.FromMilliseconds(1209)
    sub_ecu.status.progress.elapsed_time_download.FromMilliseconds(98709)
    sub_ecu.status.progress.errors_download = 109

    mocker.patch.object(ota_client_stub._ota_client_call, "status", return_value=sub)

    with grpc.insecure_channel("localhost:50051") as channel:
        request = v2.StatusRequest()
        service = v2_grpc.OtaClientServiceStub(channel)
        response = service.Status(request)

        response_exp = v2.StatusResponse()
        # "subecu" ecu
        res_ecu = response_exp.ecu.add()
        res_ecu.CopyFrom(sub_ecu)

        # "autoware" ecu
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

        response_exp.available_ecu_ids.extend(["autoware"])  # default available_ecu_ids

        assert response == response_exp

    ota_client_mock.status.assert_called_once()
