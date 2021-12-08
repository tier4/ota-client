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
        response = asyncio.run(self._stub.update(request))
        logger.info(f"{response=}")
        return response

    def Rollback(self, request, context):
        logger.info(f"{request=}")
        response = self._stub.rollback(request)
        logger.info(f"{response=}")
        return response

    def Status(self, request, context):
        response = self._stub.status(request)
        logger.info(f"{response=}")
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


if __name__ == "__main__":
    import time

    with grpc.insecure_channel(f"localhost:{cfg.SERVICE_PORT}") as channel:
        stub = v2_grpc.OtaClientServiceStub(channel)
        request = v2.StatusRequest()
        response = stub.Status(request)
        print(f"{response=}")

        request = v2.UpdateRequest()
        ecu = request.ecu.add()
        ecu.ecu_id = "autoware"
        ecu.version = "autoware.1.1"
        ecu.url = "http://192.168.56.1:8081"
        ecu.cookies = "{}"
        ecu = request.ecu.add()
        ecu.ecu_id = "perception1"
        ecu.version = "perception.1.1"
        ecu.url = "http://192.168.56.1:8081"
        ecu.cookies = "{}"
        response = stub.Update(request)
        print(f"{response=}")
        while True:
            request = v2.StatusRequest()
            response = stub.Status(request)
            print(f"{response=}")
            time.sleep(2)
