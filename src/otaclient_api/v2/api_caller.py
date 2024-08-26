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
"""OTAClient API caller implementation."""


from __future__ import annotations

import grpc.aio

from otaclient_api.v2 import otaclient_v2_pb2_grpc as pb2_grpc
from otaclient_api.v2 import types


class ECUNoResponse(Exception):
    """Raised when ECU cannot response to request on-time."""


class OTAClientCall:
    @staticmethod
    async def status_call(
        ecu_id: str,
        ecu_ipaddr: str,
        ecu_port: int,
        *,
        request: types.StatusRequest,
        timeout=None,
    ) -> types.StatusResponse:
        try:
            ecu_addr = f"{ecu_ipaddr}:{ecu_port}"
            async with grpc.aio.insecure_channel(ecu_addr) as channel:
                stub = pb2_grpc.OtaClientServiceStub(channel)
                resp = await stub.Status(request.export_pb(), timeout=timeout)
                return types.StatusResponse.convert(resp)
        except Exception as e:
            _msg = f"{ecu_id=} failed to respond to status request on-time: {e!r}"
            raise ECUNoResponse(_msg) from None

    @staticmethod
    async def update_call(
        ecu_id: str,
        ecu_ipaddr: str,
        ecu_port: int,
        *,
        request: types.UpdateRequest,
        timeout=None,
    ) -> types.UpdateResponse:
        try:
            ecu_addr = f"{ecu_ipaddr}:{ecu_port}"
            async with grpc.aio.insecure_channel(ecu_addr) as channel:
                stub = pb2_grpc.OtaClientServiceStub(channel)
                resp = await stub.Update(request.export_pb(), timeout=timeout)
                return types.UpdateResponse.convert(resp)
        except Exception as e:
            _msg = f"{ecu_id=} failed to respond to update request on-time: {e!r}"
            raise ECUNoResponse(_msg) from None

    @staticmethod
    async def rollback_call(
        ecu_id: str,
        ecu_ipaddr: str,
        ecu_port: int,
        *,
        request: types.RollbackRequest,
        timeout=None,
    ) -> types.RollbackResponse:
        try:
            ecu_addr = f"{ecu_ipaddr}:{ecu_port}"
            async with grpc.aio.insecure_channel(ecu_addr) as channel:
                stub = pb2_grpc.OtaClientServiceStub(channel)
                resp = await stub.Rollback(request.export_pb(), timeout=timeout)
                return types.RollbackResponse.convert(resp)
        except Exception as e:
            _msg = f"{ecu_id=} failed to respond to rollback request on-time: {e!r}"
            raise ECUNoResponse(_msg) from None
