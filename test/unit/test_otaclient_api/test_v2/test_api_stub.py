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

from unittest.mock import AsyncMock, MagicMock

import grpc
import pytest
from otaclient_pb2.v2 import otaclient_v2_pb2 as pb2

from otaclient.grpc.api_v2.ecu_status import LocalECUStatusNotReady
from otaclient_api.v2 import _types as api_types
from otaclient_api.v2.api_stub import OtaClientServiceV2


class _AbortError(Exception):
    """Stand-in for grpc.aio.AbortError used in tests."""


class TestOtaClientServiceV2Status:
    @pytest.fixture
    def context(self) -> AsyncMock:
        ctx = AsyncMock()
        ctx.abort.side_effect = _AbortError()
        return ctx

    async def test_status_returns_response(self, context: AsyncMock):
        inner_stub = MagicMock()
        inner_stub.status = AsyncMock(return_value=api_types.StatusResponse())

        service = OtaClientServiceV2(inner_stub)
        result = await service.Status(pb2.StatusRequest(), context)

        assert isinstance(result, pb2.StatusResponse)
        context.abort.assert_not_called()

    async def test_status_local_ecu_not_ready_aborts_unavailable(
        self, context: AsyncMock
    ):
        inner_stub = MagicMock()
        inner_stub.status = AsyncMock(
            side_effect=LocalECUStatusNotReady(
                "local ECU 'autoware' status not yet collected"
            )
        )

        service = OtaClientServiceV2(inner_stub)
        with pytest.raises(_AbortError):
            await service.Status(pb2.StatusRequest(), context)

        context.abort.assert_awaited_once()
        code, details = context.abort.await_args.args
        assert code == grpc.StatusCode.UNAVAILABLE
        assert "autoware" in details
