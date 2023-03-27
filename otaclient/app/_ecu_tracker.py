"""Tracking all child ECUs status."""
import asyncio
from copy import deepcopy
import time
from typing import Dict, List, Optional, Set, Tuple
from typing_extensions import Self

from .log_setting import get_logger
from .configs import server_cfg, config as cfg
from .ota_client_call import OtaClientCall
from .ecu_info import ECUInfo, ECUContact
from .proto import wrapper

logger = get_logger(__name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL))


class ChildECUTracker:
    NORMAL_POLLING_INTERVAL = 30  # seconds
    ACTIVE_POLLING_INTERVAL = 6  # seconds
    # timeout to treat ECU as lost if ECU keeps disconnected
    UNREACHABLE_ECU_TIMEOUT = 20 * 60  # seconds

    def __init__(self, ecu_info: ECUInfo) -> None:
        self._direct_subecu = {
            _ecu.ecu_id: _ecu for _ecu in ecu_info.iter_direct_subecu_contact()
        }
        self._direct_subecu_ids = set(ecu_info.get_available_ecu_ids())
        self._lock = asyncio.Lock()
        self._started = asyncio.Event()
        self._shutdowned = asyncio.Event()

        # ------ dynamic updated information per polling ------ #
        self._poll_interval = self.NORMAL_POLLING_INTERVAL
        self._last_updated_timestamp = 0
        # all child ECUs scope stats
        self._all_available_ecu_ids: Set[str] = set()
        self._all_ecus_status: Dict[str, wrapper.StatusResponseEcuV2] = {}
        self._all_ecus_last_checked_timestamp: Dict[str, int] = {}
        # status polling summary
        # grouped by ota_status
        self._UPDATE_ecus_id: Set[str] = set()
        self._FAILED_ecus_id: Set[str] = set()
        self._SUCCESS_ecus_id: Set[str] = set()
        # grouped by in downloading
        self._UPDATE_in_downloading_ecus_id: Set[str] = set()
        # grouped by lossing connection longer than <limit>
        self._lost_ecus_id: Set[str] = set()

    async def _poll_direct_subECU_once(self):
        poll_tasks: Dict[asyncio.Task, ECUContact] = {}
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
        # query each directly connected subECU,
        # update the all_ecu_status list
        # NOTE: if no updated status for specific ecu is available,
        #       skip updating that ecu's status
        _fut: asyncio.Future
        for _fut in await asyncio.as_completed(*poll_tasks):
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
                self._all_available_ecu_ids.add(_ecu_id)
                self._all_ecus_status[_ecu_id] = child_ecu_resp
                self._all_ecus_last_checked_timestamp[_ecu_id] = int(time.time())
            # update available_ecus_id
            self._all_available_ecu_ids.update(subecu_resp.available_ecu_ids)
        poll_tasks.clear()

        current_timestamp = int(time.time())
        self._last_updated_timestamp = current_timestamp
        # loop over all ECU's last_checked_timestamp to check the lost ECU
        for _timestamp, _ecu_id in self._all_ecus_last_checked_timestamp:
            if _timestamp + self.UNREACHABLE_ECU_TIMEOUT > current_timestamp:
                self._lost_ecus_id.add(_ecu_id)
            else:  # unconditionally remove the ecu from lost_list
                self._lost_ecus_id.discard(_ecu_id)

        # loop over all ECU's status to update updating_ecus list
        for _ecu_id, _ecu_status in self._all_ecus_status.items():
            # skip lost ECU
            if _ecu_id in self._lost_ecus_id:
                self._UPDATE_ecus_id.discard(_ecu_id)
                self._FAILED_ecus_id.discard(_ecu_id)
                self._SUCCESS_ecus_id.discard(_ecu_id)
                continue

            _ota_status = _ecu_status.ota_status
            if _ota_status is wrapper.StatusOta.UPDATING:
                self._UPDATE_ecus_id.add(_ecu_id)
                # further check the update phase
                _update_phase = _ecu_status.update_status.phase
                if _update_phase <= wrapper.UpdatePhase.DOWNLOADING_OTA_FILES:
                    self._UPDATE_in_downloading_ecus_id.add(_ecu_id)
                else:
                    self._UPDATE_in_downloading_ecus_id.discard(_ecu_id)
            elif _ota_status is wrapper.StatusOta.SUCCESS:
                self._SUCCESS_ecus_id.add(_ecu_id)
            elif _ota_status is wrapper.StatusOta.FAILURE:
                self._FAILED_ecus_id.add(_ecu_id)

    async def _loop_polling(self):
        while not self._shutdowned.is_set():
            await self._poll_direct_subECU_once()
            await asyncio.sleep(self._poll_interval)

    # properties

    @property
    def ecus_status(self) -> List[wrapper.StatusResponseEcuV2]:
        return list(self._all_ecus_status.values())

    @property
    def available_ecu_ids(self) -> List[str]:
        return self._all_available_ecu_ids.copy()

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
        """Query Whether there is at least one child ECU requires otaproxy.

        Returns:
            A tuple consists of an int of last_updated_timestamp, and a boll
                indicates whether there is at least one child ECU requires otaproxy.
        """
        return (
            self._last_updated_timestamp,
            len(self._UPDATE_in_downloading_ecus_id) > 0,
        )

    # API

    async def start(self) -> Optional[Tuple[Self, asyncio.Task]]:
        """
        NOTE: the caller has the responsibility to track the polling task.
        """
        async with self._lock:
            if self._started.is_set() or self._shutdowned.is_set():
                return
            self._started.set()
            return self, asyncio.create_task(self._loop_polling())

    def set_poll_interval(self, interval: int):
        self._poll_interval = interval

    async def shutdown(self):
        async with self._lock:
            self._shutdowned.set()
