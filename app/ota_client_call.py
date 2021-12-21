import grpc
import otaclient_v2_pb2_grpc as v2_grpc

from configs import config as cfg
import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class OtaClientCall:
    def __init__(self, port=None):
        self._port = port

    async def update(self, request, ip_addr, port=None):
        target_addr = f"{ip_addr}:{port if port else self._port}"
        async with grpc.aio.insecure_channel(target_addr) as channel:
            stub = v2_grpc.OtaClientServiceStub(channel)
            response = await stub.Update(request)
        return response

    def rollback(self, request, ip_addr, port=None):
        target_addr = f"{ip_addr}:{port if port else self._port}"
        with grpc.insecure_channel(target_addr) as channel:
            stub = v2_grpc.OtaClientServiceStub(channel)
            return stub.Rollback(request)

    def status(self, request, ip_addr, port=None):
        target_addr = f"{ip_addr}:{port if port else self._port}"
        with grpc.insecure_channel(target_addr) as channel:
            stub = v2_grpc.OtaClientServiceStub(channel)
            return stub.Status(request)

    def cancel_update(self, request, ip_addr, port=None):
        target_addr = f"{ip_addr}:{port if port else self._port}"
        with grpc.insecure_channel(target_addr) as channel:
            stub = v2_grpc.OtaClientServiceStub(channel)
            return stub.CancelUpdate(request)
