import time
import asyncio
from functools import partial

from app.ota_client_call import OtaClientCall
from app.otaclient_v2_pb2 import StatusRequest

import logutil
import logging
logger = logutil.get_logger(__name__, logging.DEBUG)

def call_status(
    caller: OtaClientCall, 
    ecu_ip: str="127.0.0.1", ecu_port: str="50051",
    interval: float=1, count: int=1024):
    request = StatusRequest()
    logger.debug(f"request status API on ecu at {ecu_ip}:{ecu_port}")

    for i in range(count):
        logger.debug(f"status request#{i}")
        time.sleep(interval)
        try:
            response = caller.status(
                request=request,
                ip_addr=ecu_ip,
                port=ecu_port,
            )
        except Exception as e:
            logger.debug(f"API request failed: {e!r}")
            continue
        logger.debug(f"{response=}")