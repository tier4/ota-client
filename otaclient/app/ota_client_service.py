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


import grpc.aio

from .proto import wrapper
from .proto import v2_grpc
from .proto import v2
from . import log_setting
from .ota_client_stub import OtaClientStub
from .configs import server_cfg, config as cfg


logger = log_setting.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class OtaClientServiceV2(v2_grpc.OtaClientServiceServicer):
    def __init__(self, ota_client_stub: OtaClientStub):
        self._stub = ota_client_stub

    async def Update(self, request: v2.UpdateRequest, context) -> v2.UpdateResponse:
        response = await self._stub.update(wrapper.UpdateRequest.wrap(request))
        return response.unwrap()  # type: ignore

    async def Rollback(
        self, request: v2.RollbackRequest, context
    ) -> v2.RollbackResponse:
        response = await self._stub.rollback(wrapper.RollbackRequest.wrap(request))
        return response.unwrap()  # type: ignore

    async def Status(self, request: v2.StatusRequest, context) -> v2.StatusResponse:
        response = await self._stub.status(wrapper.StatusRequest.wrap(request))
        return response.unwrap()  # type: ignore


async def service_start(port, service_list) -> grpc.aio.Server:
    server = grpc.aio.server()
    for service in service_list:
        service["grpc"].add_OtaClientServiceServicer_to_server(
            service["instance"], server
        )
    server.add_insecure_port(port)

    await server.start()
    return server


async def launch_otaclient_grpc_server():
    ota_client_stub = OtaClientStub()
    ota_client_service_v2 = OtaClientServiceV2(ota_client_stub)

    server = await service_start(
        f"{ota_client_stub.host_addr()}:{server_cfg.SERVER_PORT}",
        [
            {"grpc": v2_grpc, "instance": ota_client_service_v2},
        ],
    )

    await service_wait_for_termination(server)


async def service_wait_for_termination(server: grpc.aio.Server):
    await server.wait_for_termination()


async def service_stop(server: grpc.aio.Server):
    await server.stop(None)
