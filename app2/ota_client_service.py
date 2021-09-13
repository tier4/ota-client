import grpc
import otaclient_pb2
import otaclient_pb2_grpc


class OtaClientService(otaclient_pb2_grpc.OtaClientServiceServicer):
    def __init__(self, ota_client_stub):
        self._stub = ota_client_stub

    def update(self, request, context):
        result = self._stub.update(request)
        response = otaclient_pb2.UpdateResponse()
        response.result = result
        return response

    def rollback(self, request, context):
        result = self._stub.rollback(request)
        response = otaclient_pb2.RollbackResponse()
        response.result = result
        return response

    def status(self, request, context):
        result = self._stub.status(request)
        response = otaclient_pb2.StatusResponse()
        response.result = result
        return response
