import asyncio
import grpc.aio
from typing import Optional

from app.proto import otaclient_v2_pb2 as v2
from app.proto import otaclient_v2_pb2_grpc as v2_grpc
from app import log_util
from app.configs import config as cfg

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

    async def rollback(self, request, ip_addr, port=None):
        target_addr = f"{ip_addr}:{port if port else self._port}"
        async with grpc.aio.insecure_channel(target_addr) as channel:
            stub = v2_grpc.OtaClientServiceStub(channel)
            response = await stub.Rollback(request)
            return response

    async def status(self, request, ip_addr, port=None):
        target_addr = f"{ip_addr}:{port if port else self._port}"
        async with grpc.aio.insecure_channel(target_addr) as channel:
            stub = v2_grpc.OtaClientServiceStub(channel)
            response = await stub.Status(request)
            return response

    async def status_call(
        self, ecu_id: str, ecu_addr: str, *, timeout=None
    ) -> Optional[v2.StatusResponse]:
        try:
            return await asyncio.wait_for(
                self.status(v2.StatusRequest(), ecu_addr),  # type: ignore
                timeout=timeout,
            )
        except (grpc.aio.AioRpcError, asyncio.TimeoutError):
            # NOTE(20220801): for status querying, if the target ecu
            # is unreachable, just return nothing, instead of return
            # a response with result=RECOVERABLE
            pass

    async def update_call(
        self,
        ecu_id: str,
        ecu_addr: str,
        *,
        request: v2.UpdateRequest,
        timeout=None,
    ) -> v2.UpdateResponse:
        try:
            return await asyncio.wait_for(
                self.update(request, ecu_addr),  # type: ignore
                timeout=timeout,
            )
        except (grpc.aio.AioRpcError, asyncio.TimeoutError):
            resp = v2.UpdateResponse()
            ecu = resp.ecu.add()
            ecu.ecu_id = ecu_id
            ecu.result = v2.RECOVERABLE  # treat unreachable ecu as recoverable

            return resp
