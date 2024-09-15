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
"""Implementation of ECU status storage."""


from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from itertools import chain
from typing import Dict, Iterable, TypeVar

from otaclient.app.configs import config as cfg
from otaclient.app.configs import ecu_info, server_cfg
from otaclient.configs.ecu_info import ECUContact
from otaclient.stats_monitor import OTAClientStatus
from otaclient_api.v2 import types as api_types
from otaclient_api.v2.api_caller import ECUNoResponse, OTAClientCall

logger = logging.getLogger(__name__)

T = TypeVar("T")


class _OrderedSet(Dict[T, None]):
    def __init__(self, _input: Iterable[T] | None):
        if _input:
            for elem in _input:
                self[elem] = None
        super().__init__()

    def add(self, value: T):
        self[value] = None

    def remove(self, value: T):
        super().pop(value)

    def discard(self, value: T):
        super().pop(value, None)


class ECUStatusStorage:
    """Storage for holding ECU status reports from all ECUs in the cluster.

    This storage holds all ECUs' status gathered and reported by status polling tasks.
    The storage can be used to:
    1. generate response for the status API,
    2. tracking all child ECUs' status, generate overall ECU status report.

    The overall ECU status report will be generated with stored ECUs' status on
    every <PROPERTY_REFRESH_INTERVAL> seconds, if there is any update to the storage.

    Currently it will generate the following overall ECU status report:
    1. any_requires_network:        at least one ECU requires network(for downloading) during update,
    2. all_success:                 all ECUs are in SUCCESS ota_status or not,
    3. lost_ecus_id:                a list of ecu_ids of all disconnected ECUs,
    4. in_update_ecus_id:           a list of ecu_ids of all updating ECUs,
    5. failed_ecus_id:              a list of ecu_ids of all failed ECUs,
    6. success_ecus_id:             a list of ecu_ids of all succeeded ECUs,
    7. in_update_childecus_id:      a list of ecu_ids of all updating child ECUs.

    NOTE:
        If ECU has been disconnected(doesn't respond to status probing) longer than <UNREACHABLE_TIMEOUT>,
        it will be treated as UNREACHABLE and listed in <lost_ecus_id>, further being excluded when generating
        any_requires_network, all_success, in_update_ecus_id, failed_ecus_id, success_ecus_id and in_update_childecus_id.
    """

    DELAY_OVERALL_STATUS_REPORT_UPDATE = (
        cfg.KEEP_OVERALL_ECUS_STATUS_ON_ANY_UPDATE_REQ_ACKED
    )
    UNREACHABLE_ECU_TIMEOUT = cfg.ECU_UNREACHABLE_TIMEOUT
    PROPERTY_REFRESH_INTERVAL = cfg.OVERALL_ECUS_STATUS_UPDATE_INTERVAL
    IDLE_POLLING_INTERVAL = cfg.IDLE_INTERVAL
    ACTIVE_POLLING_INTERVAL = cfg.ACTIVE_INTERVAL

    # NOTE(20230522):
    #   ECU will be treated as disconnected if we cannot get in touch with it
    #   longer than <DISCONNECTED_ECU_TIMEOUT_FACTOR> * <current_polling_interval>.
    #   disconnected ECU will be excluded from status API response.
    DISCONNECTED_ECU_TIMEOUT_FACTOR = 3

    def __init__(self) -> None:
        self.my_ecu_id = ecu_info.ecu_id
        self._writer_lock = asyncio.Lock()
        # ECU status storage
        self.storage_last_updated_timestamp = 0

        # ECUs that are/will be active during an OTA session,
        #   at init it will be the ECUs listed in available_ecu_ids defined
        #   in ecu_info.yaml.
        # When receives update request, the list will be set to include ECUs
        #   listed in the update request, and be extended by merging
        #   available_ecu_ids in sub ECUs' status report.
        # Internally referenced when generating overall ECU status report.
        # TODO: in the future if otaclient can preserve OTA session info,
        #       ECUStatusStorage should restore the tracked_active_ecus info
        #       in the saved session info.
        self._tracked_active_ecus: _OrderedSet[str] = _OrderedSet(
            ecu_info.get_available_ecu_ids()
        )

        # The attribute that will be exported in status API response,
        # NOTE(20230801): available_ecu_ids only serves information purpose,
        #                 it should only be updated with ecu_info.yaml or merging
        #                 available_ecu_ids field in sub ECUs' status report.
        # NOTE(20230801): for web.auto user, available_ecu_ids in status API response
        #                 will be used to generate update request list, so be-careful!
        self._available_ecu_ids: _OrderedSet[str] = _OrderedSet(
            ecu_info.get_available_ecu_ids()
        )

        self._all_ecus_status_v2: Dict[str, api_types.StatusResponseEcuV2] = {}
        self._all_ecus_status_v1: Dict[str, api_types.StatusResponseEcu] = {}
        self._all_ecus_last_contact_timestamp: Dict[str, int] = {}

        # overall ECU status report
        self._properties_update_lock = asyncio.Lock()
        self.properties_last_update_timestamp = 0
        self.active_ota_update_present = asyncio.Event()

        self.lost_ecus_id = set()
        self.failed_ecus_id = set()

        self.in_update_ecus_id = set()
        self.in_update_child_ecus_id = set()
        self.any_requires_network = False

        self.success_ecus_id = set()
        self.all_success = False

        # property update task
        # NOTE: _debug_properties_update_shutdown_event is for test only,
        #       allow us to stop background task without changing codes.
        #       In normal running this event will never be set.
        self._debug_properties_update_shutdown_event = asyncio.Event()
        asyncio.create_task(self._loop_updating_properties())

        # on receive update request
        self.last_update_request_received_timestamp = 0

    def _is_ecu_lost(self, ecu_id: str, cur_timestamp: int) -> bool:
        if ecu_id not in self._all_ecus_last_contact_timestamp:
            return True  # we have not yet connected to this ECU
        return (
            cur_timestamp
            > self._all_ecus_last_contact_timestamp[ecu_id]
            + self.UNREACHABLE_ECU_TIMEOUT
        )

    async def _generate_overall_status_report(self):
        """Generate overall status report against tracked active OTA ECUs.

        NOTE: as special case, lost_ecus set is calculated against all reachable ECUs.
        """
        self.properties_last_update_timestamp = cur_timestamp = int(time.time())

        # check unreachable ECUs
        # NOTE(20230801): this property is calculated against all reachable ECUs,
        #                 including further child ECUs.
        _old_lost_ecus_id = self.lost_ecus_id
        self.lost_ecus_id = lost_ecus = {
            ecu_id
            for ecu_id in self._all_ecus_last_contact_timestamp
            if self._is_ecu_lost(ecu_id, cur_timestamp)
        }
        if _new_lost_ecus_id := lost_ecus.difference(_old_lost_ecus_id):
            logger.warning(f"new lost ecu(s) detected: {_new_lost_ecus_id}")
        if lost_ecus:
            logger.warning(
                f"lost ecu(s)(disconnected longer than{self.UNREACHABLE_ECU_TIMEOUT}s): {lost_ecus=}"
            )

        # check ECUs in tracked active ECUs set that are updating
        _old_in_update_ecus_id = self.in_update_ecus_id
        self.in_update_ecus_id = in_update_ecus_id = {
            status.ecu_id
            for status in chain(
                self._all_ecus_status_v2.values(), self._all_ecus_status_v1.values()
            )
            if status.ecu_id in self._tracked_active_ecus
            and status.is_in_update
            and status.ecu_id not in lost_ecus
        }
        self.in_update_child_ecus_id = in_update_ecus_id - {self.my_ecu_id}
        if _new_in_update_ecu := in_update_ecus_id.difference(_old_in_update_ecus_id):
            logger.info(
                "new ECU(s) that acks update request and enters OTA update detected"
                f"{_new_in_update_ecu}, current updating ECUs: {in_update_ecus_id}"
            )
        if in_update_ecus_id:
            self.active_ota_update_present.set()
        else:
            self.active_ota_update_present.clear()

        # check if there is any failed child/self ECU in tracked active ECUs set
        _old_failed_ecus_id = self.failed_ecus_id
        self.failed_ecus_id = {
            status.ecu_id
            for status in chain(
                self._all_ecus_status_v2.values(), self._all_ecus_status_v1.values()
            )
            if status.ecu_id in self._tracked_active_ecus
            and status.is_failed
            and status.ecu_id not in lost_ecus
        }
        if _new_failed_ecu := self.failed_ecus_id.difference(_old_failed_ecus_id):
            logger.warning(
                f"new failed ECU(s) detected: {_new_failed_ecu}, current {self.failed_ecus_id=}"
            )

        # check if any ECUs in the tracked tracked active ECUs set require network
        self.any_requires_network = any(
            (
                status.requires_network
                for status in chain(
                    self._all_ecus_status_v2.values(), self._all_ecus_status_v1.values()
                )
                if status.ecu_id in self._tracked_active_ecus
                and status.ecu_id not in lost_ecus
            )
        )

        # check if all tracked active_ota_ecus are in SUCCESS ota_status
        _old_all_success, _old_success_ecus_id = self.all_success, self.success_ecus_id
        self.success_ecus_id = {
            status.ecu_id
            for status in chain(
                self._all_ecus_status_v2.values(), self._all_ecus_status_v1.values()
            )
            if status.ecu_id in self._tracked_active_ecus
            and status.is_success
            and status.ecu_id not in lost_ecus
        }
        # NOTE: all_success doesn't count the lost ECUs
        self.all_success = len(self.success_ecus_id) == len(self._tracked_active_ecus)
        if _new_success_ecu := self.success_ecus_id.difference(_old_success_ecus_id):
            logger.info(f"new succeeded ECU(s) detected: {_new_success_ecu}")
            if not _old_all_success and self.all_success:
                logger.info("all ECUs in the cluster are in SUCCESS ota_status")

        logger.debug(
            "overall ECU status reporrt updated:"
            f"{self.lost_ecus_id=}, {self.in_update_ecus_id=},{self.any_requires_network=},"
            f"{self.failed_ecus_id=}, {self.success_ecus_id=}, {self.all_success=}"
        )

    async def _loop_updating_properties(self):
        """ECU status storage's self generating overall ECU status report task.

        NOTE:
        1. only update properties when storage is updated,
        2. if just receive update request, skip generating new overall status report
            for <DELAY> seconds to prevent pre-mature status change.
            check on_receive_update_request method below for more details.
        """
        last_storage_update = self.storage_last_updated_timestamp
        while not self._debug_properties_update_shutdown_event.is_set():
            async with self._properties_update_lock:
                current_timestamp = int(time.time())
                if last_storage_update != self.storage_last_updated_timestamp and (
                    current_timestamp
                    > self.last_update_request_received_timestamp
                    + self.DELAY_OVERALL_STATUS_REPORT_UPDATE
                ):
                    last_storage_update = self.storage_last_updated_timestamp
                    await self._generate_overall_status_report()
            # if properties are not initialized, use active_interval for update
            if self.properties_last_update_timestamp == 0:
                await asyncio.sleep(self.ACTIVE_POLLING_INTERVAL)
            else:
                await asyncio.sleep(self.PROPERTY_REFRESH_INTERVAL)

    # API

    async def update_from_child_ecu(self, status_resp: api_types.StatusResponse):
        """Update the ECU status storage with child ECU's status report(StatusResponse)."""
        async with self._writer_lock:
            self.storage_last_updated_timestamp = cur_timestamp = int(time.time())

            # NOTE: use v2 if v2 is available, but explicitly support v1 format
            #       for backward-compatible with old otaclient
            # NOTE: edge condition of ECU doing OTA downgrade to old image with old otaclient,
            #       this ECU will report status in v1 when downgrade is finished! So if we use
            #       v1 status report, we should remove the entry in v2, vice versa.
            _processed_ecus_id = set()
            for ecu_status_v2 in status_resp.iter_ecu_v2():
                ecu_id = ecu_status_v2.ecu_id
                self._all_ecus_status_v2[ecu_id] = ecu_status_v2
                self._all_ecus_last_contact_timestamp[ecu_id] = cur_timestamp
                self._all_ecus_status_v1.pop(ecu_id, None)
                _processed_ecus_id.add(ecu_id)

            for ecu_status_v1 in status_resp.iter_ecu():
                ecu_id = ecu_status_v1.ecu_id
                if ecu_id in _processed_ecus_id:
                    continue  # use v2 in prior
                self._all_ecus_status_v1[ecu_id] = ecu_status_v1
                self._all_ecus_last_contact_timestamp[ecu_id] = cur_timestamp
                self._all_ecus_status_v2.pop(ecu_id, None)

    async def update_from_local_ecu(self, ecu_status: api_types.StatusResponseEcuV2):
        """Update ECU status storage with local ECU's status report(StatusResponseEcuV2)."""
        async with self._writer_lock:
            self.storage_last_updated_timestamp = cur_timestamp = int(time.time())

            ecu_id = ecu_status.ecu_id
            self._all_ecus_status_v2[ecu_id] = ecu_status
            self._all_ecus_last_contact_timestamp[ecu_id] = cur_timestamp

    async def on_ecus_accept_update_request(self, ecus_accept_update: set[str]):
        """Update overall ECU status report directly on ECU(s) accept OTA update request.

        for the ECUs that accepts OTA update request, we:
        1. add these ECUs' id into in_update_ecus_id set
        2. remove these ECUs' id from failed_ecus_id and success_ecus_id set
        3. remove these ECUs' id from lost_ecus_id set
        3. re-calculate overall ECUs status
        4. reset tracked_active_ecus list to <ecus_accept_update>

        To prevent pre-mature overall status change(for example, the child ECU doesn't change
        their ota_status to UPDATING on-time due to status polling interval mismatch),
        the above set value will be kept for <DELAY_OVERALL_STATUS_REPORT_UPDATE> seconds.
        """
        async with self._properties_update_lock:
            self._tracked_active_ecus = _OrderedSet(ecus_accept_update)

            self.last_update_request_received_timestamp = int(time.time())
            self.lost_ecus_id -= ecus_accept_update
            self.failed_ecus_id -= ecus_accept_update

            self.in_update_ecus_id.update(ecus_accept_update)
            self.in_update_child_ecus_id = self.in_update_ecus_id - {self.my_ecu_id}

            self.any_requires_network = True
            self.all_success = False
            self.success_ecus_id -= ecus_accept_update

            self.active_ota_update_present.set()

    def get_polling_interval(self) -> int:
        """Return <ACTIVE_POLLING_INTERVAL> if there is active OTA update,
        otherwise <IDLE_POLLING_INTERVAL>.

        NOTE: use get_polling_waiter if want to wait, only call this method
            if one only wants to get the polling interval value.
        """
        return (
            self.ACTIVE_POLLING_INTERVAL
            if self.active_ota_update_present.is_set()
            else self.IDLE_POLLING_INTERVAL
        )

    def get_polling_waiter(self):
        """Get a executable async waiter which waiting time is decided by
        whether there is active OTA updating or not.

        This waiter will wait for <ACTIVE_POLLING_INTERVAL> and then return
            if there is active OTA in the cluster.
        Or if the cluster doesn't have active OTA, wait <IDLE_POLLING_INTERVAL>
            or self.active_ota_update_present is set, return when one of the
            condition is met.
        """

        async def _waiter():
            if self.active_ota_update_present.is_set():
                await asyncio.sleep(self.ACTIVE_POLLING_INTERVAL)
                return

            try:
                await asyncio.wait_for(
                    self.active_ota_update_present.wait(),
                    timeout=self.IDLE_POLLING_INTERVAL,
                )
            except asyncio.TimeoutError:
                return

        return _waiter

    async def export(self) -> api_types.StatusResponse:
        """Export the contents of this storage to an instance of StatusResponse.

        NOTE: wrapper.StatusResponse's add_ecu method already takes care of
              v1 format backward-compatibility(input v2 format will result in
              v1 format and v2 format in the StatusResponse).
        NOTE: to align with preivous behavior that disconnected ECU should have no
              entry in status API response, simulate this behavior by skipping
              disconnected ECU's status report entry.
        """
        res = api_types.StatusResponse()

        async with self._writer_lock:
            res.available_ecu_ids.extend(self._available_ecu_ids)

            # NOTE(20230802): export all reachable ECUs' status, no matter they are in
            #                 active OTA or not.
            # NOTE: ECU status for specific ECU will not appear at both v1 and v2 list,
            #       this is guaranteed by the update_from_child_ecu API method.
            for ecu_id in self._all_ecus_last_contact_timestamp:
                # NOTE: skip this ECU if it doesn't respond recently enough,
                #       to signal the agent that this ECU doesn't respond.
                _timout = (
                    self._all_ecus_last_contact_timestamp.get(ecu_id, 0)
                    + self.DISCONNECTED_ECU_TIMEOUT_FACTOR * self.get_polling_interval()
                )
                if self.storage_last_updated_timestamp > _timout:
                    continue

                _ecu_status_rep = self._all_ecus_status_v2.get(
                    ecu_id, self._all_ecus_status_v1.get(ecu_id)
                )
                if _ecu_status_rep:
                    res.add_ecu(_ecu_status_rep)
            return res


class ECUTracker:
    """Tracker that queries and stores ECU status from all defined ECUs."""

    def __init__(
        self,
        ecu_status_storage: ECUStatusStorage,
        stats_push_queue: deque[OTAClientStatus],
    ) -> None:
        self._stats_push_queue = stats_push_queue  # for local ECU status polling
        self._ecu_status_storage = ecu_status_storage
        self._polling_waiter = self._ecu_status_storage.get_polling_waiter()

        # launch ECU trackers for all defined ECUs
        # NOTE: _debug_ecu_status_polling_shutdown_event is for test only,
        #       allow us to stop background task without changing codes.
        #       In normal running this event will never be set.
        self._debug_ecu_status_polling_shutdown_event = asyncio.Event()
        asyncio.create_task(self._polling_local_ecu_status())
        for ecu_contact in ecu_info.secondaries:
            asyncio.create_task(self._polling_direct_subecu_status(ecu_contact))

    async def _polling_direct_subecu_status(self, ecu_contact: ECUContact):
        """Task entry for loop polling one subECU's status."""
        while not self._debug_ecu_status_polling_shutdown_event.is_set():
            try:
                _ecu_resp = await OTAClientCall.status_call(
                    ecu_contact.ecu_id,
                    str(ecu_contact.ip_addr),
                    ecu_contact.port,
                    timeout=server_cfg.QUERYING_SUBECU_STATUS_TIMEOUT,
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
        # TODO: covert from internal format to api types
        while not self._debug_ecu_status_polling_shutdown_event.is_set():
            with contextlib.suppress(IndexError):
                status_report = self._stats_push_queue.pop()
                await self._ecu_status_storage.update_from_local_ecu(status_report)

            await self._polling_waiter()
