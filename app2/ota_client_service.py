from concurrent import futures
from logging import getLogger

import grpc
import otaclient_pb2 as v1  # to keep backword compatibility
import otaclient_pb2_grpc as grpc_v1  # to keep backword compatibility
import otaclient_v2_pb2 as v2
import otaclient_v2_pb2_grpc as grpc_v2
import configs as cfg

logger = getLogger(__name__)
logger.setLevel(cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL))


class OtaClientServiceV2(grpc_v2.OtaClientServiceServicer):
    def __init__(self, ota_client_stub):
        self._stub = ota_client_stub
        self._server = None

    def Update(self, request, context):
        results = self._stub.update(request)
        response = v2.UpdateResponse()
        for result in results:
            response_ecu = response.update_response_ecu.add()
            response_ecu.ecu_id = result["ecu_id"]
            response_ecu.result = result["result"]
        return response

    def Rollback(self, request, context):
        result = self._stub.rollback(request)
        response = v2.RollbackResponse()
        response.result = result
        return response

    def Status(self, request, context):
        result = self._stub.status(request)
        response = v2.StatusResponse()
        response.result = result
        return response

    def service_start(self, port):
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
        grpc_v2.add_OtaClientServiceServicer_to_server(self, server)
        server.add_insecure_port(port)
        server.start()
        self._server = server

    def service_wait_for_termination(self):
        self._server.wait_for_termination()
