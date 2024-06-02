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

from typing import Any

from otaclient_api.v2 import (
    otaclient_v2_pb2_grpc as pb2_grpc,
    otaclient_v2_pb2 as pb2,
    types,
)


class OtaClientServiceV2(pb2_grpc.OtaClientServiceServicer):
    def __init__(self, ota_client_stub: Any):
        self._stub = ota_client_stub

    async def Update(self, request: pb2.UpdateRequest, context) -> pb2.UpdateResponse:
        response = await self._stub.update(types.UpdateRequest.convert(request))
        return response.export_pb()

    async def Rollback(
        self, request: pb2.RollbackRequest, context
    ) -> pb2.RollbackResponse:
        response = await self._stub.rollback(types.RollbackRequest.convert(request))
        return response.export_pb()

    async def Status(self, request: pb2.StatusRequest, context) -> pb2.StatusResponse:
        response = await self._stub.status(types.StatusRequest.convert(request))
        return response.export_pb()
