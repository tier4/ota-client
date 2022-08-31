import asyncio
import grpc.aio
from typing import Optional

from app.proto import wrapper
from app.proto import otaclient_v2_pb2 as v2
from app.proto import otaclient_v2_pb2_grpc as v2_grpc
from app import log_util
from app.configs import config as cfg
from app.configs import server_cfg

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class OtaClientCall:
    @staticmethod
    async def status_call(
        ecu_id: str,
        ecu_ipaddr: str,
        ecu_port: int = server_cfg.SERVER_PORT,
        *,
        timeout=None,
    ) -> Optional[wrapper.StatusResponse]:
        try:
            ecu_addr = f"{ecu_ipaddr}:{ecu_port}"
            async with grpc.aio.insecure_channel(ecu_addr) as channel:
                stub = v2_grpc.OtaClientServiceStub(channel)
                resp = await stub.Status(v2.StatusRequest(), timeout=timeout)
                return wrapper.StatusResponse.wrap(resp)
        except (grpc.aio.AioRpcError, asyncio.TimeoutError):
            # NOTE(20220801): for status querying, if the target ecu
            # is unreachable, just return nothing, instead of return
            # a response with result=RECOVERABLE
            logger.debug(f"{ecu_id=} failed to respond to status request on-time.")

    @staticmethod
    async def update_call(
        ecu_id: str,
        ecu_ipaddr: str,
        ecu_port: int = server_cfg.SERVER_PORT,
        *,
        request: wrapper.UpdateRequest,
        timeout=None,
    ) -> wrapper.UpdateResponse:
        try:
            ecu_addr = f"{ecu_ipaddr}:{ecu_port}"
            async with grpc.aio.insecure_channel(ecu_addr) as channel:
                stub = v2_grpc.OtaClientServiceStub(channel)
                resp = await stub.Update(request.unwrap(), timeout=timeout)
                return wrapper.UpdateResponse.wrap(resp)
        except (grpc.aio.AioRpcError, asyncio.TimeoutError):
            resp = wrapper.UpdateResponse()
            # treat unreachable ecu as recoverable
            resp.add_ecu(
                wrapper.UpdateResponseEcu(
                    ecu_id=ecu_id,
                    result=wrapper.FailureType.RECOVERABLE.value,
                )
            )
            return resp

    @staticmethod
    async def rollback_call(
        ecu_id: str,
        ecu_ipaddr: str,
        ecu_port: int = server_cfg.SERVER_PORT,
        *,
        request: wrapper.RollbackRequest,
        timeout=None,
    ) -> wrapper.RollbackResponse:
        try:
            ecu_addr = f"{ecu_ipaddr}:{ecu_port}"
            async with grpc.aio.insecure_channel(ecu_addr) as channel:
                stub = v2_grpc.OtaClientServiceStub(channel)
                resp = await stub.Rollback(request.unwrap(), timeout=timeout)
                return wrapper.RollbackResponse.wrap(resp)
        except (grpc.aio.AioRpcError, asyncio.TimeoutError):
            resp = wrapper.RollbackResponse()
            # treat unreachable ecu as recoverable
            resp.add_ecu(
                wrapper.RollbackResponseEcu(
                    ecu_id=ecu_id,
                    result=wrapper.FailureType.RECOVERABLE.value,
                )
            )
            return resp
