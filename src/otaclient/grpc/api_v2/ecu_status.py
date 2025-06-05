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


from __future__ import annotations

import asyncio
import logging
import math
import pickle
import time
from dataclasses import dataclass, field
from itertools import chain
from pathlib import Path

from otaclient._types import MultipleECUStatusFlags, OTAClientStatus
from otaclient.configs.cfg import cfg, ecu_info
from otaclient.grpc.api_v2._types import convert_to_apiv2_status
from otaclient_api.v2 import _types as api_types

logger = logging.getLogger(__name__)

# NOTE(20230522):
#   ECU will be treated as disconnected if we cannot get in touch with it
#   longer than <DISCONNECTED_ECU_TIMEOUT_FACTOR> * <current_polling_interval>.
#   disconnected ECU will be excluded from status API response.
DISCONNECTED_ECU_TIMEOUT_FACTOR = 3

IDLE_POLLING_INTERVAL = 10  # second
ACTIVE_POLLING_INTERVAL = 1  # seconds


@dataclass
class ECUStatusState:
    """State container for ECUStatusStorage."""

    # ECU status flags
    ecu_status_flags: MultipleECUStatusFlags

    # ECU status storage
    storage_last_updated_timestamp: int = 0

    # Available ECU IDs dictionary
    available_ecu_ids: dict[str, None] = field(default_factory=dict)

    # ECU status dictionaries
    all_ecus_status_v2: dict[str, api_types.StatusResponseEcuV2] = field(
        default_factory=dict
    )
    all_ecus_status_v1: dict[str, api_types.StatusResponseEcu] = field(
        default_factory=dict
    )
    all_ecus_last_contact_timestamp: dict[str, int] = field(default_factory=dict)

    # Overall ECU status report
    properties_last_update_timestamp: int = 0

    # Sets for tracking ECU states
    lost_ecus_id: set[str] = field(default_factory=set)
    failed_ecus_id: set[str] = field(default_factory=set)
    in_update_ecus_id: set[str] = field(default_factory=set)
    in_update_child_ecus_id: set[str] = field(default_factory=set)
    success_ecus_id: set[str] = field(default_factory=set)

    # Update request tracking
    last_update_request_received_timestamp: int = 0

    def to_pickle(self):
        """Serialize the ECUStatusState instance to a byte string using pickle.

        Returns:
            bytes: Pickled representation of the ECUStatusState instance
        """
        # asdict doesn't support serialization of Event objects
        # so we need to convert them to a serializable format
        # and then convert them back to Event objects during deserialization
        state_dict = {}
        for key, value in self.__dict__.items():
            if isinstance(value, MultipleECUStatusFlags):
                # Pickle doesn't support serialization of Event objects
                flags_state = {
                    flag_name: getattr(value, flag_name).is_set()
                    for flag_name in dir(value)
                    if isinstance(getattr(value, flag_name, None), asyncio.Event)
                }
                state_dict[key] = flags_state
            else:
                state_dict[key] = value
        return pickle.dumps(state_dict)

    def from_pickle(self, pickled_data):
        """Deserialize a pickled byte string to restore the ECUStatusState instance.

        Args:
            pickled_data (bytes): Pickled representation of the ECUStatusState instance.
        """
        state_dict = pickle.loads(pickled_data)
        for key, value in state_dict.items():
            # Check if the value is of type MultipleECUStatusFlags
            if isinstance(getattr(self, key, None), MultipleECUStatusFlags):
                flags_instance = getattr(self, key)
                for flag_name in dir(flags_instance):
                    flag = getattr(flags_instance, flag_name, None)
                    if isinstance(flag, asyncio.Event):
                        flag_value = value.get(flag_name, False)
                        if flag_value:
                            flag.set()
                        else:
                            flag.clear()
            else:
                setattr(self, key, value)


class ECUStatusStorage:
    def __init__(
        self,
        *,
        ecu_status_flags: MultipleECUStatusFlags,
    ) -> None:
        self.my_ecu_id = ecu_info.ecu_id
        self._writer_lock = asyncio.Lock()

        # The attribute that will be exported in status API response,
        # NOTE(20230801): for web.auto user, available_ecu_ids in status API response
        #                 will be used to generate update request list, so be-careful!
        # NOTE(20241219): we will only look at status of ECUs listed in available_ecu_ids.
        #                 ECUs that in the secondaries field but no in available_ecu_ids field
        #                 are considered to be the ECUs not ready for OTA. See ecu_info.yaml doc.
        # Initialize the state dataclass
        self._state = ECUStatusState(ecu_status_flags=ecu_status_flags)
        # Initialize available_ecu_ids with the list from ecu_info
        self._state.available_ecu_ids = dict.fromkeys(ecu_info.get_available_ecu_ids())

        # Overall ECU status report lock
        self._properties_update_lock = asyncio.Lock()

        # property update task
        # NOTE: _debug_properties_update_shutdown_event is for test only,
        #       allow us to stop background task without changing codes.
        #       In normal running this event will never be set.
        self._debug_properties_update_shutdown_event = asyncio.Event()
        asyncio.create_task(self._loop_updating_properties())

    def _is_ecu_lost(self, ecu_id: str, cur_timestamp: int) -> bool:
        if ecu_id not in self._state.all_ecus_last_contact_timestamp:
            return True  # we have not yet connected to this ECU
        return (
            cur_timestamp
            > self._state.all_ecus_last_contact_timestamp[ecu_id]
            + cfg.ECU_UNREACHABLE_TIMEOUT
        )

    async def _generate_overall_status_report(self):
        """Generate overall status report against tracked active OTA ECUs.

        NOTE: as special case, lost_ecus set is calculated against all reachable ECUs.
        """
        self._state.properties_last_update_timestamp = cur_timestamp = int(time.time())
        ecu_status_flags = self._state.ecu_status_flags

        # check unreachable ECUs
        # NOTE(20230801): this property is calculated against all reachable ECUs,
        #                 including further child ECUs.
        _old_lost_ecus_id = self._state.lost_ecus_id
        self._state.lost_ecus_id = lost_ecus = {
            ecu_id
            for ecu_id in self._state.all_ecus_last_contact_timestamp
            if self._is_ecu_lost(ecu_id, cur_timestamp)
        }
        if _new_lost_ecus_id := lost_ecus.difference(_old_lost_ecus_id):
            logger.warning(f"new lost ecu(s) detected: {_new_lost_ecus_id}")
        if lost_ecus:
            logger.warning(
                f"lost ecu(s)(disconnected longer than{cfg.ECU_UNREACHABLE_TIMEOUT}s): {lost_ecus=}"
            )

        # check ECUs in tracked active ECUs set that are updating
        _old_in_update_ecus_id = self._state.in_update_ecus_id
        self._state.in_update_ecus_id = in_update_ecus_id = {
            status.ecu_id
            for status in chain(
                self._state.all_ecus_status_v2.values(),
                self._state.all_ecus_status_v1.values(),
            )
            if status.ecu_id in self._state.available_ecu_ids
            and (status.is_in_update or status.is_in_client_update)
            and status.ecu_id not in lost_ecus
        }
        self._state.in_update_child_ecus_id = in_update_ecus_id - {self.my_ecu_id}
        if _new_in_update_ecu := in_update_ecus_id.difference(_old_in_update_ecus_id):
            logger.info(
                "new ECU(s) that acks update request and enters OTA update detected"
                f"{_new_in_update_ecu}, current updating ECUs: {in_update_ecus_id}"
            )

        if self._state.in_update_child_ecus_id:
            ecu_status_flags.any_child_ecu_in_update.set()
        else:
            ecu_status_flags.any_child_ecu_in_update.clear()

        # check if there is any failed child/self ECU in tracked active ECUs set
        _old_failed_ecus_id = self._state.failed_ecus_id
        self._state.failed_ecus_id = {
            status.ecu_id
            for status in chain(
                self._state.all_ecus_status_v2.values(),
                self._state.all_ecus_status_v1.values(),
            )
            if status.ecu_id in self._state.available_ecu_ids
            and status.is_failed
            and status.ecu_id not in lost_ecus
        }
        if _new_failed_ecu := self._state.failed_ecus_id.difference(
            _old_failed_ecus_id
        ):
            logger.warning(
                f"new failed ECU(s) detected: {_new_failed_ecu}, current {self._state.failed_ecus_id=}"
            )

        # check if any ECUs in the tracked tracked active ECUs set require network
        if any(
            (
                status.requires_network
                for status in chain(
                    self._state.all_ecus_status_v2.values(),
                    self._state.all_ecus_status_v1.values(),
                )
                if status.ecu_id in self._state.available_ecu_ids
                and status.ecu_id not in lost_ecus
            )
        ):
            ecu_status_flags.any_requires_network.set()
        else:
            ecu_status_flags.any_requires_network.clear()

        # check if all tracked active_ota_ecus are in SUCCESS ota_status
        _old_all_success = ecu_status_flags.all_success.is_set()
        _old_success_ecus_id = self._state.success_ecus_id

        self._state.success_ecus_id = {
            status.ecu_id
            for status in chain(
                self._state.all_ecus_status_v2.values(),
                self._state.all_ecus_status_v1.values(),
            )
            if status.ecu_id in self._state.available_ecu_ids
            and status.is_success
            and status.ecu_id not in lost_ecus
        }
        # NOTE: all_success doesn't count the lost ECUs
        if self._state.success_ecus_id == set(self._state.available_ecu_ids):
            ecu_status_flags.all_success.set()
        else:
            ecu_status_flags.all_success.clear()

        if _new_success_ecu := self._state.success_ecus_id.difference(
            _old_success_ecus_id
        ):
            logger.info(f"new succeeded ECU(s) detected: {_new_success_ecu}")
            if ecu_status_flags.all_success.is_set() and not _old_all_success:
                logger.info("all ECUs in the cluster are in SUCCESS ota_status")

    async def _loop_updating_properties(self):
        """ECU status storage's self generating overall ECU status report task.

        NOTE:
        1. only update properties when storage is updated,
        2. if just receive update request, skip generating new overall status report
            for <DELAY> seconds to prevent pre-mature status change.
            check on_receive_update_request method below for more details.
        """
        last_storage_update = self._state.storage_last_updated_timestamp
        while not self._debug_properties_update_shutdown_event.is_set():
            async with self._properties_update_lock:
                current_timestamp = int(time.time())
                if (
                    last_storage_update != self._state.storage_last_updated_timestamp
                    and (
                        current_timestamp
                        > self._state.last_update_request_received_timestamp
                        + cfg.PAUSED_OVERALL_ECUS_STATUS_CHANGE_ON_UPDATE_REQ_ACKED
                    )
                ):
                    last_storage_update = self._state.storage_last_updated_timestamp
                    await self._generate_overall_status_report()
            # if properties are not initialized, use active_interval for update
            if self._state.properties_last_update_timestamp == 0:
                await asyncio.sleep(ACTIVE_POLLING_INTERVAL)
            else:
                await asyncio.sleep(cfg.OVERALL_ECUS_STATUS_UPDATE_INTERVAL)

    # API

    async def update_from_child_ecu(self, status_resp: api_types.StatusResponse):
        """Update the ECU status storage with child ECU's status report(StatusResponse)."""
        async with self._writer_lock:
            self._state.storage_last_updated_timestamp = cur_timestamp = int(
                time.time()
            )

            # NOTE: use v2 if v2 is available, but explicitly support v1 format
            #       for backward-compatible with old otaclient
            # NOTE: edge condition of ECU doing OTA downgrade to old image with old otaclient,
            #       this ECU will report status in v1 when downgrade is finished! So if we use
            #       v1 status report, we should remove the entry in v2, vice versa.
            _processed_ecus_id = set()
            for ecu_status_v2 in status_resp.iter_ecu_v2():
                ecu_id = ecu_status_v2.ecu_id
                self._state.all_ecus_status_v2[ecu_id] = ecu_status_v2
                self._state.all_ecus_last_contact_timestamp[ecu_id] = cur_timestamp
                self._state.all_ecus_status_v1.pop(ecu_id, None)
                _processed_ecus_id.add(ecu_id)

            for ecu_status_v1 in status_resp.iter_ecu():
                ecu_id = ecu_status_v1.ecu_id
                if ecu_id in _processed_ecus_id:
                    continue  # use v2 in prior
                self._state.all_ecus_status_v1[ecu_id] = ecu_status_v1
                self._state.all_ecus_last_contact_timestamp[ecu_id] = cur_timestamp
                self._state.all_ecus_status_v2.pop(ecu_id, None)

    async def update_from_local_ecu(self, local_status: OTAClientStatus):
        """Update ECU status storage with local ECU's status report(StatusResponseEcuV2)."""
        async with self._writer_lock:
            self._state.storage_last_updated_timestamp = cur_timestamp = int(
                time.time()
            )

            ecu_id = self.my_ecu_id
            self._state.all_ecus_status_v2[ecu_id] = convert_to_apiv2_status(
                local_status
            )
            self._state.all_ecus_last_contact_timestamp[ecu_id] = cur_timestamp

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
        ecu_status_flags = self._state.ecu_status_flags
        async with self._properties_update_lock:
            self._state.last_update_request_received_timestamp = int(time.time())
            self._state.lost_ecus_id -= ecus_accept_update
            self._state.failed_ecus_id -= ecus_accept_update
            self._state.success_ecus_id -= ecus_accept_update

            self._state.in_update_ecus_id.update(ecus_accept_update)
            self._state.in_update_child_ecus_id = self._state.in_update_ecus_id - {
                self.my_ecu_id
            }

            ecu_status_flags.all_success.clear()
            ecu_status_flags.any_requires_network.set()
            if self._state.in_update_child_ecus_id:
                ecu_status_flags.any_child_ecu_in_update.set()
            else:
                ecu_status_flags.any_child_ecu_in_update.clear()

    def get_polling_interval(self) -> int:
        """Return <ACTIVE_POLLING_INTERVAL> if there is active OTA update,
        otherwise <IDLE_POLLING_INTERVAL>.

        NOTE: use get_polling_waiter if want to wait, only call this method
            if one only wants to get the polling interval value.
        """
        return (
            ACTIVE_POLLING_INTERVAL
            if self._state.in_update_ecus_id
            else IDLE_POLLING_INTERVAL
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
        # waiter closure will slice the waiting time by <_inner_wait_interval>,
        #   add wait each slice one by one while checking the ecu_status_flags.
        _inner_wait_interval = 1  # second

        async def _waiter():
            if self._state.in_update_ecus_id:
                await asyncio.sleep(ACTIVE_POLLING_INTERVAL)
                return

            for _ in range(math.ceil(IDLE_POLLING_INTERVAL / _inner_wait_interval)):
                if self._state.in_update_ecus_id:
                    return
                await asyncio.sleep(_inner_wait_interval)

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
            res.available_ecu_ids.extend(self._state.available_ecu_ids)

            # NOTE(20230802): export all reachable ECUs' status, no matter they are in
            #                 active OTA or not.
            # NOTE: ECU status for specific ECU will not appear at both v1 and v2 list,
            #       this is guaranteed by the update_from_child_ecu API method.
            for ecu_id in self._state.all_ecus_last_contact_timestamp:
                # NOTE: skip this ECU if it doesn't respond recently enough,
                #       to signal the agent that this ECU doesn't respond.
                _timout = (
                    self._state.all_ecus_last_contact_timestamp.get(ecu_id, 0)
                    + DISCONNECTED_ECU_TIMEOUT_FACTOR * self.get_polling_interval()
                )
                if self._state.storage_last_updated_timestamp > _timout:
                    continue

                _ecu_status_rep = self._state.all_ecus_status_v2.get(
                    ecu_id, self._state.all_ecus_status_v1.get(ecu_id)
                )
                if _ecu_status_rep:
                    res.add_ecu(_ecu_status_rep)
            return res

    def save_state(self) -> None:
        """Save the current state of the ECU status storage to a file."""
        # TODO: should protect the pickled status with encryption key
        # can be passed down to os.execve launched otaclient via env)
        _pickle = self._state.to_pickle()
        _path = Path(cfg.OTACLIENT_STATUS_FILE)
        with open(_path, "wb") as f:
            f.write(_pickle)
        logger.info(f"saved ECU status storage to {_path}")

    def load_state(self) -> bool:
        """Load the state of the ECU status storage from a file."""
        _path = Path(cfg.OTACLIENT_STATUS_FILE)
        if not _path.exists():
            logger.warning(f"file {_path} does not exist, skipping loading state.")
            return False

        with open(_path, "rb") as f:
            _pickle = f.read()
        self._state.from_pickle(_pickle)
        logger.info(f"Loaded ECU status storage from {_path}")
        return True
