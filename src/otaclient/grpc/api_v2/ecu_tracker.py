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
import contextlib
import logging

from otaclient._utils import SharedOTAClientStatusReader
from otaclient.configs import ECUContact
from otaclient.configs.cfg import cfg, ecu_info
from otaclient.grpc.api_v2.ecu_status import ECUStatusStorage
from otaclient_api.v2 import types as api_types
from otaclient_api.v2.api_caller import ECUNoResponse, OTAClientCall

logger = logging.getLogger(__name__)

_otaclient_shutdown = False
_shm_status: SharedOTAClientStatusReader | None = None


def _global_shutdown():
    global _otaclient_shutdown
    _otaclient_shutdown = True

    if _shm_status:
        _shm_status.atexit()


atexit.register(_global_shutdown)


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

        global _shm_status
        _shm_status = local_ecu_status_reader

    async def _polling_direct_subecu_status(self, ecu_contact: ECUContact):
        """Task entry for loop polling one subECU's status."""
        while not _otaclient_shutdown:
            try:
                _ecu_resp = await OTAClientCall.status_call(
                    ecu_contact.ecu_id,
                    str(ecu_contact.ip_addr),
                    ecu_contact.port,
                    timeout=cfg.QUERYING_SUBECU_STATUS_TIMEOUT,
                    request=api_types.StatusRequest(),
                )
                await self._ecu_status_storage.update_from_child_ecu(_ecu_resp)
            except ECUNoResponse as e:
                logger.debug(
                    f"ecu@{ecu_contact} doesn't respond to status request: {e!r}"
                )
            await self._polling_waiter()

    async def _polling_local_ecu_status(self):
        """Task entry for loop polling local ECU status."""
        while not _otaclient_shutdown:
            with contextlib.suppress(Exception):
                status_report = self._local_ecu_status_reader.sync_msg()
                await self._ecu_status_storage.update_from_local_ecu(status_report)
            await self._polling_waiter()

    def start(self) -> None:
        asyncio.create_task(self._polling_local_ecu_status())
        for ecu_contact in ecu_info.secondaries:
            asyncio.create_task(self._polling_direct_subecu_status(ecu_contact))
