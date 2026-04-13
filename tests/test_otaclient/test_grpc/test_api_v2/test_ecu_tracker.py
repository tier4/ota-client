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

import asyncio
import ipaddress
from unittest.mock import AsyncMock, MagicMock

from otaclient.configs._ecu_info import ECUContact
from otaclient.grpc.api_v2.ecu_tracker import ECUTracker
from otaclient_api.v2 import _types as api_types

ECU_TRACKER_MODULE = "otaclient.grpc.api_v2.ecu_tracker"


async def test_startup_matrix_cleared_on_ecu_v2_response(mocker):
    """_polling_direct_subecu_status clears startup_matrix when ECU responds with V2."""
    ecu_contact = ECUContact(
        ecu_id="p1", ip_addr=ipaddress.IPv4Address("127.0.0.1"), port=50051
    )
    resp = api_types.StatusResponse(
        available_ecu_ids=["p1"],
        ecu_v2=[
            api_types.StatusResponseEcuV2(
                ecu_id="p1",
                ota_status=api_types.StatusOta.SUCCESS,
            )
        ],
    )

    # make status_call return the V2 response immediately
    mocker.patch(
        f"{ECU_TRACKER_MODULE}.OTAClientCall.status_call",
        AsyncMock(return_value=resp),
    )

    mock_storage = MagicMock()
    mock_storage.update_from_child_ecu = AsyncMock()

    # waiter blocks so the loop pauses after the first iteration, allowing cancellation
    async def _blocking_waiter():
        await asyncio.sleep(60)

    mock_storage.get_polling_waiter = MagicMock(return_value=lambda: _blocking_waiter())

    mock_reader = MagicMock()
    mock_reader.atexit = MagicMock()

    tracker = ECUTracker(mock_storage, local_ecu_status_reader=mock_reader)
    assert tracker._startup_matrix["p1"] is True

    task = asyncio.create_task(tracker._polling_direct_subecu_status(ecu_contact))
    # wait until the first iteration finishes and the loop blocks on the waiter
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert tracker._startup_matrix["p1"] is False
