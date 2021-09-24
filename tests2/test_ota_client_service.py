import os
import pytest
import grpc


@pytest.fixture
def start_service_with_ota_client_mock(mocker):
    from ota_client_service import (
        OtaClientServiceV2,
        OtaClientService,
        service_start,
        service_stop,
    )
    from ota_client_stub import OtaClientStub
    from ota_client import OtaClient
    import otaclient_v2_pb2 as v2
    import otaclient_v2_pb2_grpc as v2_grpc
    import otaclient_pb2 as v1
    import otaclient_pb2_grpc as v1_grpc

    ota_client_mock = mocker.Mock(spec=OtaClient)
    mocker.patch("ota_client_stub.OtaClient", return_value=ota_client_mock)

    ota_client_stub = OtaClientStub()

    ota_client_service_v2 = OtaClientServiceV2(ota_client_stub)
    ota_client_service = OtaClientService(ota_client_stub)

    server = service_start(
        "localhost:50051",
        [
            {"grpc": v1_grpc, "instance": ota_client_service},
            {"grpc": v2_grpc, "instance": ota_client_service_v2},
        ],
    )

    yield ota_client_mock

    service_stop(server)


def test_ota_client_service_update(mocker, start_service_with_ota_client_mock):
    import otaclient_v2_pb2 as v2
    import otaclient_v2_pb2_grpc as v2_grpc

    ota_client_mock = start_service_with_ota_client_mock

    with grpc.insecure_channel("localhost:50051") as channel:
        request = v2.UpdateRequest()
        req_ecu = request.ecu.add()
        req_ecu.ecu_id = "autoware"
        req_ecu.version = "1.2.3.a"
        req_ecu.url = "https://foo.bar.com/ota-data"
        req_ecu.cookies = '{"test": "my data"}'
        service = v2_grpc.OtaClientServiceStub(channel)
        response = service.Update(request)

        resopnse_exp = v2.UpdateResponse()
        res_ecu = resopnse_exp.ecu.add()
        res_ecu.ecu_id = "autoware"
        res_ecu.result = v2.Result.OK
        # TODO:
        # assert results

    ota_client_mock.update.assert_called_once_with(
        "1.2.3.a", "https://foo.bar.com/ota-data", '{"test": "my data"}'
    )


def test_ota_client_service_rollback(mocker, start_service_with_ota_client_mock):
    import otaclient_v2_pb2 as v2
    import otaclient_v2_pb2_grpc as v2_grpc

    ota_client_mock = start_service_with_ota_client_mock

    with grpc.insecure_channel("localhost:50051") as channel:
        request = v2.RollbackRequest()
        ecu = request.ecu.add()
        ecu.ecu_id = "autoware"
        service = v2_grpc.OtaClientServiceStub(channel)
        results = service.Rollback(request)

    ota_client_mock.rollback.assert_called_once()


def test_ota_client_service_status(mocker, start_service_with_ota_client_mock):
    import otaclient_v2_pb2 as v2
    import otaclient_v2_pb2_grpc as v2_grpc

    ota_client_mock = start_service_with_ota_client_mock

    with grpc.insecure_channel("localhost:50051") as channel:
        request = v2.StatusRequest()
        service = v2_grpc.OtaClientServiceStub(channel)
        results = service.Status(request)

    ota_client_mock.status.assert_called_once()
