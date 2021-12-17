from concurrent import futures

import grpc
import asyncio
import otaclient_v2_pb2 as v2
import otaclient_v2_pb2_grpc as v2_grpc
from configs import config as cfg
import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class OtaClientServiceV2(v2_grpc.OtaClientServiceServicer):
    def __init__(self, ota_client_stub):
        self._stub = ota_client_stub
        self._server = None

    def Update(self, request, context):
        logger.info(f"{request=}")
        results = asyncio.run(self._stub.update(request))
        logger.info(f"{results=}")
        return results

    def Rollback(self, request, context):
        logger.info(f"{request=}")
        results = self._stub.rollback(request)
        logger.info(f"{results=}")
        return results

    def Status(self, request, context):
        result = asyncio.run(self._stub.status(request))
        logger.info(f"{result=}")
        return result

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
