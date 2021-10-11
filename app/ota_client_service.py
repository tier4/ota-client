from concurrent import futures
from logging import getLogger

import grpc
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
        # TODO: conver grpc request to dict
        results = self._stub.update(request)
        response = v2.UpdateResponse()
        for result in results:
            res_ecu = response.ecu.add()
            res_ecu.ecu_id = result["ecu_id"]
            res_ecu.result = result["result"]
        return response

    def Rollback(self, request, context):
        # TODO: conver grpc request to dict
        results = self._stub.rollback(request)
        response = v2.RollbackResponse()
        for result in results:
            res_ecu = response.ecu.add()
            res_ecu.ecu_id = result["ecu_id"]
            res_ecu.result = result["result"]
        return response

    def Status(self, request, context):
        # TODO: conver grpc request to dict
        results = self._stub.status(request)
        response = v2.StatusResponse()

        for result in results:
            res_ecu = response.ecu.add()
            res_ecu.ecu_id = result["ecu_id"]
            res_ecu.result = result["result"]
            status = result.get("status")
            if status:
                # ecu.status
                es = res_ecu.status
                es.status = v2.StatusOta.Value(status["status"])
                es.failure = v2.FailureType.Value(status["failure_type"])
                es.failure_reason = status["failure_reason"]
                es.version = status["version"]

                # ecu.status.progress
                esp = es.progress
                progress = status["update_progress"]
                esp.phase = v2.StatusProgressPhase.Value(progress["phase"])
                esp.total_regular_files = progress["total_regular_files"]
                esp.regular_files_processed = progress["regular_files_processed"]
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
