import asyncio
import yaml
import json
import grpc

from app import otaclient_v2_pb2_grpc as v2_grpc
from app.otaclient_v2_pb2 import UpdateRequest, UpdateRequestEcu

import logutil
import logging
logger = logutil.get_logger(__name__, logging.DEBUG)

_default_request = UpdateRequest(
        ecu=[UpdateRequestEcu(
            ecu_id="autoware",
            version="123.x",
            url="http://10.6.65.3:8080",
            cookies=json.dumps({"test": "my-cookie"}),
        )],
    )

def load_external_update_request(request_yaml_file: str) -> UpdateRequest:
    with open(request_yaml_file, "r") as f:
        request_yaml: dict = yaml.safe_load(f)
        logger.debug(f"load external request: {request_yaml!r}")

        request = UpdateRequest(
        ecu=[UpdateRequestEcu(
            ecu_id=request_yaml.get("ecu_id","autoware"),
            version=request_yaml.get("version", "123.x"),
            url=request_yaml.get("url", "http://10.6.65.3:8080"),
            cookies=json.dumps(
                request_yaml.get("cookies", '{"test": "my-cookie"}')
                ),
        )],
    )

    return request

async def _update(request, ip_addr, port="50051"):
    target_addr = f"{ip_addr}:{port}"
    async with grpc.aio.insecure_channel(target_addr) as channel:
        stub = v2_grpc.OtaClientServiceStub(channel)
        response = await stub.Update(request)
    return response

def call_update(
    ecu_ip: str="127.0.0.1", ecu_port: str="50051",
    request: UpdateRequest=_default_request):
    logger.debug(f"request update on ecu at {ecu_ip}:{ecu_port}")
    logger.debug(f"{request=}")

    try:
        result = asyncio.run(_update(
            request=request,
            ip_addr=ecu_ip,
            port=ecu_port,
        ))
        logger.debug(f"{result=}")
    except Exception as e:
        logger.debug(f"error occured: {e!r}")
