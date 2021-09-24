from concurrent import futures
from logging import getLogger

import grpc
import otaclient_pb2 as v1  # to keep backward compatibility
import otaclient_pb2_grpc as v1_grpc  # to keep backward compatibility
import otaclient_v2_pb2 as v2
import otaclient_v2_pb2_grpc as v2_grpc
import configs as cfg

logger = getLogger(__name__)
logger.setLevel(cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL))


class OtaClientServiceV2(v2_grpc.OtaClientServiceServicer):
    def __init__(self, ota_client_stub):
        self._stub = ota_client_stub
        self._server = None

    def Update(self, request, context):
        results = self._stub.update(request)
        response = v2.UpdateResponse()
        for result in results:
            res_ecu = response.ecu.add()
            res_ecu.ecu_id = result["ecu_id"]
            res_ecu.result = result["result"]
        return response

    def Rollback(self, request, context):
        results = self._stub.rollback(request)
        response = v2.RollbackResponse()
        for result in results:
            res_ecu = response.ecu.add()
            res_ecu.ecu_id = result["ecu_id"]
            res_ecu.result = result["result"]
        return response

    def Status(self, request, context):
        results = self._stub.status(request)
        response = v2.StatusResponse()
        for result in results:
            res_ecu = response.ecu.add()
            res_ecu.ecu_id = result["ecu_id"]
            res_ecu.result = result["result"]
        return response

    def service_start(self, port):
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
        v2_grpc.add_OtaClientServiceServicer_to_server(self, server)
        server.add_insecure_port(port)
        server.start()
        self._server = server

    def service_wait_for_termination(self):
        self._server.wait_for_termination()

    def service_stop(self):
        self._server.stop(None)


# DEPRECATED
class OtaClientService(v1_grpc.OtaClientServiceServicer):
    def __init__(self, ota_client_stub):
        self._stub = ota_client_stub
        self._server = None

    def OtaUpdate(self, request, context):
        # TODO: convert v1 request to v2 request
        results = self._stub.update(request)
        response = v1.OtaUpdateReply()
        # TODO: convert v2 result to v1 response
        """
        for result in results:
            res_ecu = response.ecu.add()
            res_ecu.ecu_id = result["ecu_id"]
            res_ecu.result = result["result"]
        """
        return response

    def OtaRollback(self, request, context):
        return OtaRollbackReply()

    def EcuStatus(self, request, context):
        # TODO: convert v1 request to v2 request
        results = self._stub.status(request)
        response = v1.EcuStatusReply()
        # TODO: convert v2 result to v1 response
        """
        for result in results:
            res_ecu = response.ecu.add()
            res_ecu.ecu_id = result["ecu_id"]
            res_ecu.result = result["result"]
        """
        return response

    def EcuVersion(self, request, context):
        # TODO: convert v1 request to v2 request
        results = self._stub.status(request)
        response = v1.EcuVersionReply()
        # TODO: convert v2 result to v1 response
        """
        for result in results:
            res_ecu = response.ecu.add()
            res_ecu.ecu_id = result["ecu_id"]
            res_ecu.result = result["result"]
        """
        return response


def service_start(port, service_list):
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    for service in service_list:
        service["grpc"].add_OtaClientServiceServicer_to_server(
            service["instance"], server
        )
    server.add_insecure_port(port)
    server.start()
    return server


def service_wait_for_termination(server):
    server.wait_for_termination()


def service_stop(server):
    server.stop(None)
