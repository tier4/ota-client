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


"""Tracking all child ECUs status."""
import asyncio
import time
from typing import Dict, Set

from .log_setting import get_logger
from .configs import server_cfg, config as cfg
from .ota_client_call import OtaClientCall
from .ecu_info import ECUInfo, ECUContact
from .proto import wrapper

logger = get_logger(__name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL))


class ECUStatusStorage:
    PROPERTY_REFRESH_INTERVAL = 6  # seconds
    UNREACHABLE_ECU_TIMEOUT = 10 * 60  # seconds

    def __init__(self) -> None:
        self._writer_lock = asyncio.Lock()
        self._all_available_ecus_id: Set[str] = set()
        self._all_ecus_status: Dict[str, wrapper.StatusResponseEcuV2] = {}
        self._all_ecus_last_contact_timestamp: Dict[str, int] = {}

        # properties cache
        self.properties_last_updated_timestamp = 0
        self.lost_ecus_id = set()
        self.any_in_update = False
        self.any_failed = False
        self.any_requires_network = False

        # property update task
        self.properties_update_shutdown_event = asyncio.Event()
        self._properties_update_task = asyncio.create_task(
            self._loop_updating_properties()
        )

    def _is_ecu_lost(self, ecu_id: str, *, cur_timestamp: int) -> bool:
        return (
            cur_timestamp
            > self._all_ecus_last_contact_timestamp[ecu_id]
            + self.UNREACHABLE_ECU_TIMEOUT
        )

    async def _properties_update(self):
        cur_timestamp = int(time.time())
        self.properties_last_updated_timestamp = cur_timestamp

        # update lost ecu list
        lost_ecus = set()
        for ecu_id in self._all_available_ecus_id:
            if self._is_ecu_lost(ecu_id, cur_timestamp=cur_timestamp):
                lost_ecus.add(ecu_id)
        # add ECUs that never appear
        lost_ecus.add(self._all_available_ecus_id - set(self._all_ecus_status))
        self.lost_ecus_id = list(lost_ecus)

        self.any_in_update = any(
            (
                status.is_in_update
                for status in self._all_ecus_status.values()
                if status.ecu_id not in lost_ecus
            )
        )
        self.any_failed = any(
            (
                status.is_failed
                for status in self._all_ecus_status.values()
                if status.ecu_id not in lost_ecus
            )
        )
        self.any_requires_network = any(
            (
                status.if_requires_network
                for status in self._all_ecus_status.values()
                if status.ecu_id not in lost_ecus
            )
        )

    async def _loop_updating_properties(self):
        while not self.properties_update_shutdown_event.is_set():
            await self._properties_update()
            # reduce polling interval on active updating
            # TODO: interval configuration
            if self.any_in_update:
                await asyncio.sleep(6)
            else:
                await asyncio.sleep(20)

    # API

    async def update_from_child_ECU(self, status_resp: wrapper.StatusResponse):
        cur_timestamp = int(time.time())
        async with self._writer_lock:
            self.properties_last_updated_timestamp = cur_timestamp
            self._all_available_ecus_id.update(status_resp.available_ecu_ids)

            ecu_using_v2 = set()
            for ecu_status in status_resp.ecu_v2:
                ecu_id = ecu_status.ecu_id
                self._all_ecus_status[ecu_id] = ecu_status
                self._all_ecus_last_contact_timestamp[ecu_id] = cur_timestamp
                ecu_using_v2.add(ecu_id)

            # if status report for specific ecu only available in v1 format,
            # convert it to v2 format and then record it
            for ecu_status_v1 in status_resp.ecu:
                ecu_id = ecu_status_v1.ecu_id
                if ecu_id not in ecu_using_v2:
                    ecu_status = wrapper.StatusResponseEcuV2.convert_from_v1(
                        ecu_status_v1
                    )
                    self._all_ecus_status[ecu_id] = ecu_status
                    self._all_ecus_last_contact_timestamp[ecu_id] = cur_timestamp

    async def update_from_local_ECU(self, ecu_status: wrapper.StatusResponseEcuV2):
        cur_timestamp = int(time.time())
        async with self._writer_lock:
            self.properties_last_updated_timestamp = cur_timestamp
            ecu_id = ecu_status.ecu_id
            self._all_ecus_status[ecu_id] = ecu_status
            self._all_ecus_last_contact_timestamp[ecu_id] = cur_timestamp


class SubECUTracker:
    """Loop polling ECU status from directly connected ECUs."""

    NORMAL_INTERVAL = 30  # seconds
    ACTIVE_INTERVAL = cfg.STATS_COLLECT_INTERVAL  # seconds
    # timeout to treat ECU as lost if ECU keeps disconnected
    UNREACHABLE_ECU_TIMEOUT = 10 * 60  # seconds

    def __init__(self, ecu_info: ECUInfo, storage: ECUStatusStorage) -> None:
        self._direct_subecu = {
            _ecu.ecu_id: _ecu for _ecu in ecu_info.iter_direct_subecu_contact()
        }
        self._storage = storage

        self.polling_shutdown_event = asyncio.Event()
        self._polling_task = asyncio.create_task(self._polling())

    async def _polling(self):
        while not self.polling_shutdown_event.is_set():
            await self._poll_direct_subECU_once()
            # TODO: configuration
            if self._storage.any_in_update:
                await asyncio.sleep(1)
            else:
                await asyncio.sleep(20)

    async def _poll_direct_subECU_once(self):
        poll_tasks: Dict[asyncio.Future, ECUContact] = {}
        for _, ecu_contact in self._direct_subecu.items():
            _task = asyncio.create_task(
                OtaClientCall.status_call(
                    ecu_contact.ecu_id,
                    ecu_contact.host,
                    ecu_contact.port,
                    timeout=server_cfg.SERVER_PORT,
                )
            )
            poll_tasks[_task] = ecu_contact

        _fut: asyncio.Future
        for _fut in asyncio.as_completed(*poll_tasks):
            ecu_contact = poll_tasks[_fut]
            try:
                subecu_resp: wrapper.StatusResponse = _fut.result()
                assert subecu_resp
            except Exception as e:
                logger.debug(f"failed to contact ecu@{ecu_contact=}: {e!r}")
                continue
            await self._storage.update_from_child_ECU(subecu_resp)
        poll_tasks.clear()
