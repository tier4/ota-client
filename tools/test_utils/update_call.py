import asyncio
import json
import grpc

from app import otaclient_v2_pb2_grpc as v2_grpc
from app.otaclient_v2_pb2 import UpdateRequest, UpdateRequestEcu

import logutil
import logging

logger = logutil.get_logger(__name__, logging.DEBUG)

_default_request = UpdateRequest(
    ecu=[
        UpdateRequestEcu(
            ecu_id="autoware",
            version="123.x",
            url="http://10.6.65.3:8080",
            cookies=json.dumps({"test": "my-cookie"}),
        )
    ],
)


async def _update(request, ip_addr, port="50051"):
    target_addr = f"{ip_addr}:{port}"
    async with grpc.aio.insecure_channel(target_addr) as channel:
        stub = v2_grpc.OtaClientServiceStub(channel)
        response = await stub.Update(request)
    return response


def call_update(
    ecu_ip: str = "127.0.0.1",
    ecu_port: str = "50051",
    request: UpdateRequest = _default_request,
):
    logger.debug(f"request update on ecu at {ecu_ip}:{ecu_port}")
    logger.debug(f"{request=}")

    try:
        result = asyncio.run(
            _update(
                request=request,
                ip_addr=ecu_ip,
                port=ecu_port,
            )
        )
        logger.debug(f"{result=}")
    except Exception as e:
        logger.debug(f"error occured: {e!r}")
