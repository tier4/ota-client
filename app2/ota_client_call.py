from logging import getLogger
from typing import Any

import grpc
import asyncio
import otaclient_pb2
import otaclient_pb2_grpc

import configs as cfg

logger = getLogger(__name__)
logger.setLevel(cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL))


class OtaClientCall:
    def __init__(self, port):
        self._port = port

    async def update(self, request, ip_addr, port=None) -> Any:
        target_addr = f"{ip_addr}:{port if port else self._port}"
        async with grpc.aio.insecure_channel(target_addr) as channel:
            stub = otaclient_pb2_grpc.OtaClientServiceStub(channel)
            response = await stub.update(request)
        return response

    def rollback(self, request, ip_addr, port=None):
        target_addr = f"{ip_addr}:{port if port else self._port}"
        with grpc.insecure_channel(target_addr) as channel:
            stub = otaclient_pb2_grpc.OtaClientServiceStub(channel)
            return stub.rollback(request)

    def status(self, request, ip_addr, port=None):
        target_addr = f"{ip_addr}:{port if port else self._port}"
        with grpc.insecure_channel(target_addr) as channel:
            stub = otaclient_pb2_grpc.OtaClientServiceStub(channel)
            return stub.status(request)
