import time
import asyncio
import grpc

from app import otaclient_v2_pb2_grpc as v2_grpc
from app.otaclient_v2_pb2 import StatusRequest

import logutil
import logging

logger = logutil.get_logger(__name__, logging.DEBUG)


async def _status(request, ip_addr, port="50051"):
    target_addr = f"{ip_addr}:{port}"
    async with grpc.aio.insecure_channel(target_addr) as channel:
        stub = v2_grpc.OtaClientServiceStub(channel)
        response = await stub.Status(request)
        return response


def call_status(
    ecu_ip: str = "127.0.0.1",
    ecu_port: str = "50051",
    interval: float = 1,
    count: int = 1024,
):
    request = StatusRequest()
    logger.debug(f"request status API on ecu at {ecu_ip}:{ecu_port}")

    for i in range(count):
        logger.debug(f"status request#{i}")
        time.sleep(interval)
        try:
            response = asyncio.run(
                _status(
                    request=request,
                    ip_addr=ecu_ip,
                    port=ecu_port,
                )
            )
        except Exception as e:
            logger.debug(f"API request failed: {e!r}")
            continue
        logger.debug(f"{response=}")
