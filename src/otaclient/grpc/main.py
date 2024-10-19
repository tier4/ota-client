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
"""OTA grpc server launcher.

NOTE: currently we only support OTA service API v2.
"""


from __future__ import annotations

import asyncio
import logging

import grpc.aio

from otaclient.app.configs import config as cfg
from otaclient.app.configs import ecu_info, server_cfg
from otaclient.grpc.api_v2.servicer import OTAClientAPIServicer
from otaclient.grpc.utils import check_other_otaclient, create_otaclient_rundir
from otaclient_api.v2 import otaclient_v2_pb2_grpc as v2_grpc
from otaclient_api.v2.api_stub import OtaClientServiceV2

logger = logging.getLogger(__name__)


def create_otaclient_grpc_server():
    service_stub = OTAClientAPIServicer()
    ota_client_service_v2 = OtaClientServiceV2(service_stub)

    server = grpc.aio.server()
    v2_grpc.add_OtaClientServiceServicer_to_server(
        server=server, servicer=ota_client_service_v2
    )
    server.add_insecure_port(f"{ecu_info.ip_addr}:{server_cfg.SERVER_PORT}")
    return server


async def launch_otaclient_grpc_server():
    server = create_otaclient_grpc_server()
    await server.start()
    await server.wait_for_termination()


def main():
    # setup the otaclient runtime working dir
    create_otaclient_rundir(cfg.RUN_DIR)

    # start the otaclient grpc server
    check_other_otaclient(cfg.OTACLIENT_PID_FILE)
    asyncio.run(launch_otaclient_grpc_server())