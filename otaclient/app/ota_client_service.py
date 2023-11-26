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


from __future__ import annotations
import grpc.aio

from .configs import config as cfg, service_config
from .ecu_info import ECUInfo
from .log_setting import get_logger
from .proto import wrapper, v2, v2_grpc
from .ota_client_stub import OTAClientServiceStub

logger = get_logger(__name__)


class OtaClientServiceV2(v2_grpc.OtaClientServiceServicer):
    def __init__(self, ota_client_stub: OTAClientServiceStub):
        self._stub = ota_client_stub

    async def Update(self, request: v2.UpdateRequest, context) -> v2.UpdateResponse:
        response = await self._stub.update(wrapper.UpdateRequest.convert(request))
        return response.export_pb()

    async def Rollback(
        self, request: v2.RollbackRequest, context
    ) -> v2.RollbackResponse:
        response = await self._stub.rollback(wrapper.RollbackRequest.convert(request))
        return response.export_pb()

    async def Status(self, request: v2.StatusRequest, context) -> v2.StatusResponse:
        response = await self._stub.status(wrapper.StatusRequest.convert(request))
        return response.export_pb()


def create_otaclient_grpc_server():
    ecu_info = ECUInfo.parse_ecu_info(cfg.ECU_INFO_FPATH)

    service_stub = OTAClientServiceStub(ecu_info=ecu_info)
    ota_client_service_v2 = OtaClientServiceV2(service_stub)

    server = grpc.aio.server()
    v2_grpc.add_OtaClientServiceServicer_to_server(
        server=server, servicer=ota_client_service_v2
    )

    listen_addr = ecu_info.ip_addr
    if service_config.SERVER_ADDRESS:  # for advanced use case only
        listen_addr = service_config.SERVER_ADDRESS
    listen_port = service_config.SERVER_PORT

    listen_info = f"{listen_addr}:{listen_port}"
    logger.info(f"create OTA grpc server at {listen_info}")

    server.add_insecure_port(listen_info)
    return server


async def launch_otaclient_grpc_server():
    server = create_otaclient_grpc_server()
    await server.start()
    await server.wait_for_termination()
