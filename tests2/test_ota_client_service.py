import os
import pytest
import grpc


def test_ota_client_service_update(mocker):
    from ota_client_service import OtaClientServiceV2
    from ota_client_stub import OtaClientStub
    from ota_client import OtaClient
    import otaclient_v2_pb2 as v2
    import otaclient_v2_pb2_grpc as v2_grpc

    ota_client_mock = mocker.Mock(spec=OtaClient)
    mocker.patch("ota_client_stub.OtaClient", return_value=ota_client_mock)

    ota_client_stub = OtaClientStub()
    ota_client_service = OtaClientServiceV2(ota_client_stub)
    ota_client_service.service_start("localhost:50051")

    with grpc.insecure_channel("localhost:50051") as channel:
        request = v2.UpdateRequest()
        request_ecu = request.update_request_ecu.add()
        request_ecu.ecu_id = "autoware"
        request_ecu.version = "1.2.3.a"
        request_ecu.url = "https://foo.bar.com/ota-data"
        request_ecu.cookies = '{"test": "my data"}'
        stub = v2_grpc.OtaClientServiceStub(channel)
        results = stub.Update(request)

    ota_client_mock.update.assert_called_once_with(
        "1.2.3.a", "https://foo.bar.com/ota-data", '{"test": "my data"}'
    )
