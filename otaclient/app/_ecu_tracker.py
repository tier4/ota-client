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
from typing import Callable, Dict, List, Optional, Set, Tuple

from .log_setting import get_logger
from .configs import server_cfg, config as cfg
from .ota_client_call import OtaClientCall
from .ecu_info import ECUInfo, ECUContact
from .proto import wrapper

logger = get_logger(__name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL))


class PollingTask:
    def __init__(
        self, *, poll_interval: float, poll_once_timeout: Optional[float] = None
    ) -> None:
        self.poll_interval = poll_interval
        self.poll_once_timeout = poll_once_timeout
        self.shutdown_event = asyncio.Event()
        self._fut = None

    def set_interval(self, interval: float):
        self.poll_interval = interval

    def start(self, poll_executable: Callable):
        if self._fut or self.shutdown_event.is_set():
            return

        async def _inner():
            while not self.shutdown_event.is_set():
                try:
                    await asyncio.wait_for(
                        poll_executable(), timeout=self.poll_once_timeout
                    )
                except asyncio.TimeoutError:
                    pass
                await asyncio.sleep(self.poll_interval)

        self._fut = asyncio.create_task(_inner())

    async def shutdown(self):
        self.shutdown_event.set()
        if self._fut:
            self._fut.cancel()  # cancel it unconditionally
            try:
                await self._fut
            except Exception:
                pass
            self._fut = None


class ChildECUTracker:
    NORMAL_INTERVAL = 30  # seconds
    ACTIVE_INTERVAL = cfg.STATS_COLLECT_INTERVAL  # seconds
    # timeout to treat ECU as lost if ECU keeps disconnected
    UNREACHABLE_ECU_TIMEOUT = 10 * 60  # seconds

    def __init__(self, ecu_info: ECUInfo) -> None:
        self.ecu_info = ecu_info
        self._direct_subecu = {
            _ecu.ecu_id: _ecu for _ecu in ecu_info.iter_direct_subecu_contact()
        }
        self._direct_subecu_ids = set(ecu_info.get_available_ecu_ids())
        # remove self ECU as we only track child ECU here
        self._direct_subecu_ids.discard(ecu_info.ecu_id)

        # ------ dynamic updated information per polling ------ #
        self._writer_lock = asyncio.Lock()
        self._last_updated_timestamp = 0
        # all child ECUs scope stats
        self._all_available_child_ecus_id: Set[str] = set()
        self._all_child_ecus_status: Dict[str, wrapper.StatusResponseEcuV2] = {}
        self._all_child_ecus_last_contact_timestamp: Dict[str, int] = {}
        # status summary
        # grouped by ota_status
        self._UPDATE_ecus_id: Set[str] = set()
        self._FAILURE_ecus_id: Set[str] = set()
        self._SUCCESS_ecus_id: Set[str] = set()
        # grouped by in downloading
        self._UPDATE_in_downloading_ecus_id: Set[str] = set()
        # grouped by lossing connection longer than <limit>
        self._lost_ecus_id: Set[str] = set()

        # ------ tracker fut ------ #
        self._polling_task = self._start_polling_task()

    def _start_polling_task(self) -> PollingTask:
        """Create and schedule async background polling task.

        Returns:
            An instance of PollingTask, which can be used to control the
                polling task(set polling interval, shutdown).
        """
        polling_task = PollingTask(poll_interval=self.NORMAL_INTERVAL)
        polling_task.start(self.poll_once)
        return polling_task

    async def _poll_direct_subECU_once(self):
        current_timestamp = int(time.time())
        self._last_updated_timestamp = current_timestamp

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
        # ------ query each directly connected subECU ------ #
        # NOTE: if no updated status for specific ecu is available,
        #       skip updating that ecu's status
        _fut: asyncio.Future
        for _fut in asyncio.as_completed(*poll_tasks):
            ecu_contact = poll_tasks[_fut]
            try:
                subecu_resp: wrapper.StatusResponse = _fut.result()
                assert subecu_resp
            except Exception as e:
                logger.debug(f"failed to contact ecu@{ecu_contact=}: {e!r}")
                continue
            # loop over all ecus in this subecu's response
            for child_ecu_resp in subecu_resp.iter_ecu_status_v2():
                _ecu_id = child_ecu_resp.ecu_id
                self._all_child_ecus_status[_ecu_id] = child_ecu_resp
                self._all_child_ecus_last_contact_timestamp[_ecu_id] = current_timestamp
            self._all_available_child_ecus_id.update(subecu_resp.available_ecu_ids)
        poll_tasks.clear()

        # ------ check lost ECU ------ #
        lost_ecus_id = set()
        for (
            _ecu_id,
            _ecu_last_update,
        ) in self._all_child_ecus_last_contact_timestamp.items():
            if _ecu_last_update + self.UNREACHABLE_ECU_TIMEOUT > current_timestamp:
                lost_ecus_id.add(_ecu_id)
        # add ECUs that never appear
        lost_ecus_id.add(
            self._all_available_child_ecus_id - set(self._all_child_ecus_status)
        )
        if lost_ecus_id:
            logger.error(
                f"ECUs that disconnect longer than {self.UNREACHABLE_ECU_TIMEOUT} seconds: {self._lost_ecus_id=}"
            )
        self._lost_ecus_id = lost_ecus_id

        # ------ update summary ------ #
        UPDATE_ecus_id = set()
        SUCCESS_ecus_id = set()
        FAILURE_ecus_id = set()
        UPDATE_in_downloading_ecus_id = set()
        for _ecu_id, _ecu_status in self._all_child_ecus_status.items():
            # skip lost ECU
            if _ecu_id in lost_ecus_id:
                continue

            if _ecu_status.is_in_update:
                UPDATE_ecus_id.add(_ecu_id)
                if _ecu_status.is_updating_and_requires_network:
                    UPDATE_in_downloading_ecus_id.add(_ecu_id)
            elif _ecu_status.is_success:
                SUCCESS_ecus_id.add(_ecu_id)
            elif _ecu_status.is_failed:
                FAILURE_ecus_id.add(_ecu_id)

        self._UPDATE_ecus_id = UPDATE_ecus_id
        self._SUCCESS_ecus_id = SUCCESS_ecus_id
        self._FAILURE_ecus_id = FAILURE_ecus_id
        self._UPDATE_in_downloading_ecus_id = UPDATE_in_downloading_ecus_id

    # properties

    @property
    def any_in_update(self) -> Tuple[int, bool]:
        """Query whether there is at least one child ECU is updating.

        Returns:
            A tuple consists of an int of last_updated_timestamp, and a bool
                indicates whether there is at least one child ECU updating or not.
        """
        return self._last_updated_timestamp, len(self._UPDATE_ecus_id) > 0

    @property
    def any_requires_otaproxy(self) -> Tuple[int, bool]:
        """Query whether there is at least one child ECU requires otaproxy.

        Returns:
            A tuple consists of an int of last_updated_timestamp, and a boll
                indicates whether there is at least one child ECU requires otaproxy.
        """
        return (
            self._last_updated_timestamp,
            len(self._UPDATE_in_downloading_ecus_id) > 0,
        )

    @property
    def any_failed_ecu(self) -> Tuple[int, bool]:
        """Query whether there is at least one failed ECU."""
        return self._last_updated_timestamp, len(self._FAILURE_ecus_id) > 0

    # API

    async def poll_once(self):
        """Poll all direct connected subECU and self ECU once.

        Gathers status reports from all directly connected subECUs and self,
            and then parses and updates the internal summary stats.
        """
        async with self._writer_lock:
            await self._poll_direct_subECU_once()

    def get_status_list(self) -> List[wrapper.StatusResponseEcuV2]:
        return list(self._all_child_ecus_status.values())

    def get_available_child_ecu_ids(self) -> List[str]:
        return list(self._all_available_child_ecus_id)

    def set_polling_interval(self, interval: float):
        if self._polling_task:
            self._polling_task.set_interval(interval=interval)

    async def shutdown_polling_task(self):
        if self._polling_task:
            await self._polling_task.shutdown()
            self._polling_task = None
