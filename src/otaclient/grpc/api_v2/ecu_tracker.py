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
"""Tracker that queries and stores ECU status from all defined ECUs."""


from __future__ import annotations

import asyncio
import atexit
import logging
from collections import defaultdict

from otaclient._utils import SharedOTAClientStatusReader
from otaclient.configs import ECUContact
from otaclient.configs.cfg import cfg, ecu_info
from otaclient.grpc.api_v2.ecu_status import ECUStatusStorage
from otaclient_api.v2 import types as api_types
from otaclient_api.v2.api_caller import ECUNoResponse, OTAClientCall
from otaclient_common.logging import BurstSuppressFilter

logger = logging.getLogger(__name__)
burst_suppressed_logger = logging.getLogger(f"{__name__}.local_ecu_check")
# NOTE: for request_error, only allow max 6 lines of logging per 30 seconds
burst_suppressed_logger.addFilter(
    BurstSuppressFilter(
        f"{__name__}.local_ecu_check",
        upper_logger_name=__name__,
        burst_round_length=30,
        burst_max=6,
    )
)

# actively polling ECUs status until we get the first valid response
#   when otaclient is just starting.
_ACTIVE_POLL_SUB_ON_STARTUP = 1
_ACTIVE_POLL_LOCAL_ON_STARTUP = 0.1


class ECUTracker:

    def __init__(
        self,
        ecu_status_storage: ECUStatusStorage,
        /,
        local_ecu_status_reader: SharedOTAClientStatusReader,
    ) -> None:
        self._local_ecu_status_reader = local_ecu_status_reader
        self._ecu_status_storage = ecu_status_storage
        self._polling_waiter = self._ecu_status_storage.get_polling_waiter()
        self._startup_matrix: defaultdict[str, bool] = defaultdict(lambda: True)

        atexit.register(local_ecu_status_reader.atexit)

    async def _polling_direct_subecu_status(self, ecu_contact: ECUContact):
        """Task entry for loop polling one subECU's status."""
        this_ecu_id = ecu_contact.ecu_id
        while True:
            try:
                _ecu_resp = await OTAClientCall.status_call(
                    ecu_contact.ecu_id,
                    str(ecu_contact.ip_addr),
                    ecu_contact.port,
                    timeout=cfg.QUERYING_SUBECU_STATUS_TIMEOUT,
                    request=api_types.StatusRequest(),
                )
                if self._startup_matrix[this_ecu_id] and (
                    _ecu_resp.find_ecu_v2(this_ecu_id)
                    or _ecu_resp.find_ecu(this_ecu_id)
                ):
                    self._startup_matrix[this_ecu_id] = False
                await self._ecu_status_storage.update_from_child_ecu(_ecu_resp)
            except ECUNoResponse as e:
                logger.debug(
                    f"ecu@{ecu_contact} doesn't respond to status request: {e!r}"
                )

            if self._startup_matrix[this_ecu_id]:
                await asyncio.sleep(_ACTIVE_POLL_SUB_ON_STARTUP)
            else:
                await self._polling_waiter()

    async def _polling_local_ecu_status(self):
        """Task entry for loop polling local ECU status."""
        my_ecu_id = ecu_info.ecu_id
        while True:
            try:
                status_report = self._local_ecu_status_reader.sync_msg()
                if status_report:
                    self._startup_matrix[my_ecu_id] = False
                await self._ecu_status_storage.update_from_local_ecu(status_report)
            except Exception as e:
                burst_suppressed_logger.debug(
                    f"failed to query local ECU's status: {e!r}"
                )

            if self._startup_matrix[my_ecu_id]:
                await asyncio.sleep(_ACTIVE_POLL_LOCAL_ON_STARTUP)
            else:
                await self._polling_waiter()

    def start(self) -> None:
        asyncio.create_task(self._polling_local_ecu_status())
        for ecu_contact in ecu_info.secondaries:
            asyncio.create_task(self._polling_direct_subecu_status(ecu_contact))
