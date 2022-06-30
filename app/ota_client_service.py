import grpc.aio
from concurrent import futures

import app.otaclient_v2_pb2_grpc as v2_grpc
import app.otaclient_v2_pb2 as v2
from app import log_util
from app.ota_client_stub import OtaClientStub
from app.configs import config as cfg

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class OtaClientServiceV2(v2_grpc.OtaClientServiceServicer):
    def __init__(self, ota_client_stub: OtaClientStub):
        self._stub = ota_client_stub

    async def Update(self, request: v2.UpdateRequest, context) -> v2.UpdateResponse:
        logger.info(f"{request=}")
        response = await self._stub.update(request)
        logger.info(f"{response=}")
        return response

    def Rollback(self, request: v2.RollbackRequest, context) -> v2.RollbackResponse:
        logger.info(f"{request=}")
        response = self._stub.rollback(request)
        logger.info(f"{response=}")
        return response

    async def Status(self, request: v2.StatusRequest, context) -> v2.StatusResponse:
        return await self._stub.status(request)


async def service_start(port, service_list) -> grpc.aio.Server:
    server = grpc.aio.server(
        migration_thread_pool=futures.ThreadPoolExecutor(max_workers=2)
    )
    for service in service_list:
        service["grpc"].add_OtaClientServiceServicer_to_server(
            service["instance"], server
        )
    server.add_insecure_port(port)

    await server.start()
    return server


async def service_wait_for_termination(server: grpc.aio.Server):
    await server.wait_for_termination()


async def service_stop(server: grpc.aio.Server):
    await server.stop(None)
