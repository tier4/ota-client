# Copyright 2022 TIER IV, INC. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Entrypoint of otaclient."""


from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from queue import Queue

import grpc.aio

from otaclient import __version__
from otaclient.configs.cfg import cfg, ecu_info, proxy_info
from otaclient.grpc.api_v2.ecu_status import ECUStatusStorage
from otaclient.grpc.api_v2.ecu_tracker import ECUTracker
from otaclient.grpc.api_v2.servicer import OTAClientAPIServicer
from otaclient.log_setting import configure_logging
from otaclient.ota_core import OTAClient, OTAClientControlFlags
from otaclient.status_monitor import OTAClientStatusCollector
from otaclient.utils import check_other_otaclient, create_otaclient_rundir
from otaclient_api.v2 import otaclient_v2_pb2_grpc as v2_grpc
from otaclient_api.v2.api_stub import OtaClientServiceV2

# configure logging before any code being executed
configure_logging()
logger = logging.getLogger(__name__)


async def create_otaclient_grpc_server():
    _executor = ThreadPoolExecutor(thread_name_prefix="otaclient_main")
    _control_flag = OTAClientControlFlags()

    status_report_queue = Queue()
    status_collector = OTAClientStatusCollector(status_report_queue)

    ecu_status_storage = ECUStatusStorage()
    ecu_tracker = ECUTracker(
        ecu_status_storage,
        local_status_collector=status_collector,
    )
    ecu_tracker.start()

    otaclient_inst = OTAClient(
        control_flags=_control_flag,
        proxy=proxy_info.get_proxy_for_local_ota(),
        status_report_queue=status_report_queue,
    )
    status_collector.start()

    service_stub = OTAClientAPIServicer(
        otaclient_inst,
        ecu_status_storage,
        control_flag=_control_flag,
        executor=_executor,
    )
    ota_client_service_v2 = OtaClientServiceV2(service_stub)
    server = grpc.aio.server()
    v2_grpc.add_OtaClientServiceServicer_to_server(
        server=server, servicer=ota_client_service_v2
    )
    server.add_insecure_port(f"{ecu_info.ip_addr}:{cfg.OTA_API_SERVER_PORT}")
    return server


async def launch_otaclient_grpc_server():
    server = await create_otaclient_grpc_server()
    await server.start()
    try:
        await server.wait_for_termination()
    finally:
        await server.stop(1)


def main():
    logger.info("started")
    logger.info(f"otaclient version: {__version__}")
    logger.info(f"ecu_info.yaml: \n{ecu_info}")

    check_other_otaclient(cfg.OTACLIENT_PID_FILE)
    create_otaclient_rundir(cfg.RUN_DIR)

    asyncio.run(launch_otaclient_grpc_server())
