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

from otaclient_api.v2 import _types
from otaclient_api.v2 import otaclient_v2_pb2_grpc as pb2_grpc


class ECUNoResponse(Exception):
    """Raised when ECU cannot response to request on-time."""


class OTAClientCall:
    @staticmethod
    async def status_call(
        ecu_id: str,
        ecu_ipaddr: str,
        ecu_port: int,
        *,
        request: _types.StatusRequest,
        timeout=None,
    ) -> _types.StatusResponse:
        try:
            ecu_addr = f"{ecu_ipaddr}:{ecu_port}"
            async with grpc.aio.insecure_channel(ecu_addr) as channel:
                stub = pb2_grpc.OtaClientServiceStub(channel)
                resp = await stub.Status(request.export_pb(), timeout=timeout)
                return _types.StatusResponse.convert(resp)
        except Exception as e:
            _msg = f"{ecu_id=} failed to respond to status request on-time: {e!r}"
            raise ECUNoResponse(_msg) from e

    @staticmethod
    async def update_call(
        ecu_id: str,
        ecu_ipaddr: str,
        ecu_port: int,
        *,
        request: _types.UpdateRequest,
        timeout=None,
    ) -> _types.UpdateResponse:
        try:
            ecu_addr = f"{ecu_ipaddr}:{ecu_port}"
            async with grpc.aio.insecure_channel(ecu_addr) as channel:
                stub = pb2_grpc.OtaClientServiceStub(channel)
                resp = await stub.Update(request.export_pb(), timeout=timeout)
                return _types.UpdateResponse.convert(resp)
        except Exception as e:
            _msg = f"{ecu_id=} failed to respond to update request on-time: {e!r}"
            raise ECUNoResponse(_msg) from e

    @staticmethod
    async def rollback_call(
        ecu_id: str,
        ecu_ipaddr: str,
        ecu_port: int,
        *,
        request: _types.RollbackRequest,
        timeout=None,
    ) -> _types.RollbackResponse:
        try:
            ecu_addr = f"{ecu_ipaddr}:{ecu_port}"
            async with grpc.aio.insecure_channel(ecu_addr) as channel:
                stub = pb2_grpc.OtaClientServiceStub(channel)
                resp = await stub.Rollback(request.export_pb(), timeout=timeout)
                return _types.RollbackResponse.convert(resp)
        except Exception as e:
            _msg = f"{ecu_id=} failed to respond to rollback request on-time: {e!r}"
            raise ECUNoResponse(_msg) from e

    @staticmethod
    async def client_update_call(
        ecu_id: str,
        ecu_ipaddr: str,
        ecu_port: int,
        *,
        request: _types.ClientUpdateRequest,
        timeout=None,
    ) -> _types.ClientUpdateResponse:
        try:
            ecu_addr = f"{ecu_ipaddr}:{ecu_port}"
            async with grpc.aio.insecure_channel(ecu_addr) as channel:
                stub = pb2_grpc.OtaClientServiceStub(channel)
                resp = await stub.ClientUpdate(request.export_pb(), timeout=timeout)
                return _types.ClientUpdateResponse.convert(resp)
        except Exception as e:
            _msg = (
                f"{ecu_id=} failed to respond to client update request on-time: {e!r}"
            )
            raise ECUNoResponse(_msg) from e
