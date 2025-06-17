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
"""Shared utils for boot_controller."""


from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Callable, Optional, Union

from otaclient._types import OTAStatus
from otaclient.configs.cfg import cfg
from otaclient_common._io import read_str_from_file, write_str_to_file_atomic

logger = logging.getLogger(__name__)

FinalizeSwitchBootFunc = Callable[[], bool]


class OTAStatusFilesControl:
    """Logics for controlling otaclient's OTA status with corresponding files.

    OTAStatus files:
        status: current slot's OTA status
        slot_in_use: expected active slot
        version: image version in current slot

    If status or slot_in_use file is missing, initialization is required.
    status will be set to INITIALIZED, and slot_in_use will be set to current slot.
    """

    def __init__(
        self,
        *,
        active_slot: str,
        standby_slot: str,
        current_ota_status_dir: Union[str, Path],
        standby_ota_status_dir: Union[str, Path],
        finalize_switching_boot: FinalizeSwitchBootFunc,
        force_initialize: bool = False,
    ) -> None:
        self.active_slot = active_slot
        self.standby_slot = standby_slot
        self.current_ota_status_dir = Path(current_ota_status_dir)
        self.standby_ota_status_dir = Path(standby_ota_status_dir)
        self.finalize_switching_boot = finalize_switching_boot

        self._force_initialize = force_initialize
        self.current_ota_status_dir.mkdir(exist_ok=True, parents=True)
        self._load_slot_in_use_file()
        self._load_status_file()
        logger.info(
            f"ota_status files parsing completed, ota_status is {self._ota_status.name}"
        )

    def _load_status_file(self):
        """Check and/or init ota_status files for current slot."""
        _loaded_ota_status = self._load_current_status()
        if self._force_initialize:
            _loaded_ota_status = None

        # initialize ota_status files if not presented/incompleted/invalid
        if _loaded_ota_status is None:
            logger.info(
                "ota_status files incompleted/not presented, "
                f"initializing and set/store status to {OTAStatus.INITIALIZED.name}..."
            )
            self._store_current_status(OTAStatus.INITIALIZED)
            self._ota_status = OTAStatus.INITIALIZED
            return
        logger.info(f"status loaded from file: {_loaded_ota_status.name}")

        # status except UPDATING and ROLLBACKING(like SUCCESS/FAILURE/ROLLBACK_FAILURE)
        # are remained as it
        if _loaded_ota_status not in [OTAStatus.UPDATING, OTAStatus.ROLLBACKING]:
            self._ota_status = _loaded_ota_status
            return

        # updating or rollbacking,
        # NOTE: pre-assign live ota_status with the loaded ota_status before entering finalizing_switch_boot,
        #       as some of the platform might have slow finalizing process(like raspberry).
        self._ota_status = _loaded_ota_status
        # if is_switching_boot, execute the injected finalize_switching_boot function from
        # boot controller, transit the ota_status according to the execution result.
        # NOTE(20230614): for boot controller during multi-stage reboot(like rpi_boot),
        #                 calling finalize_switching_boot might result in direct reboot,
        #                 in such case, otaclient will terminate and ota_status will not be updated.
        if self._is_switching_boot(self.active_slot):
            if self.finalize_switching_boot():
                self._ota_status = OTAStatus.SUCCESS
                self._store_current_status(OTAStatus.SUCCESS)
            else:
                self._ota_status = (
                    OTAStatus.ROLLBACK_FAILURE
                    if _loaded_ota_status == OTAStatus.ROLLBACKING
                    else OTAStatus.FAILURE
                )
                self._store_current_status(self._ota_status)
                logger.error(
                    "finalization failed on first reboot during switching boot"
                )

        else:
            logger.error(
                f"we are in {_loaded_ota_status.name} ota_status, "
                "but ota_status files indicate that we are not in switching boot mode, "
                "this indicates a failed first reboot"
            )
            self._ota_status = (
                OTAStatus.ROLLBACK_FAILURE
                if _loaded_ota_status == OTAStatus.ROLLBACKING
                else OTAStatus.FAILURE
            )
            self._store_current_status(self._ota_status)

    def _load_slot_in_use_file(self):
        _loaded_slot_in_use = self._load_current_slot_in_use()
        if self._force_initialize:
            _loaded_slot_in_use = None

        if not _loaded_slot_in_use:
            # NOTE(20230831): this can also resolve the backward compatibility issue
            #                 in is_switching_boot method when old otaclient doesn't create
            #                 slot_in_use file.
            self._store_current_slot_in_use(self.active_slot)
            return
        logger.info(f"slot_in_use loaded from file: {_loaded_slot_in_use}")

        # check potential failed switching boot
        if _loaded_slot_in_use != self.active_slot:
            logger.warning(
                f"boot into old slot {self.active_slot}, "
                f"but slot_in_use indicates it should boot into {_loaded_slot_in_use}, "
                "this might indicate a failed finalization at first reboot after update/rollback"
            )

    # slot_in_use control

    def _store_current_slot_in_use(self, _slot: str):
        write_str_to_file_atomic(
            self.current_ota_status_dir / cfg.SLOT_IN_USE_FNAME, _slot
        )

    def _store_standby_slot_in_use(self, _slot: str):
        write_str_to_file_atomic(
            self.standby_ota_status_dir / cfg.SLOT_IN_USE_FNAME, _slot
        )

    def _load_current_slot_in_use(self) -> Optional[str]:
        if res := read_str_from_file(
            self.current_ota_status_dir / cfg.SLOT_IN_USE_FNAME, _default=""
        ):
            return res

    # status control

    def _store_current_status(self, _status: OTAStatus):
        write_str_to_file_atomic(
            self.current_ota_status_dir / cfg.OTA_STATUS_FNAME, _status.name
        )

    def _store_standby_status(self, _status: OTAStatus):
        write_str_to_file_atomic(
            self.standby_ota_status_dir / cfg.OTA_STATUS_FNAME, _status.name
        )

    def _load_current_status(self) -> Optional[OTAStatus]:
        if _status_str := read_str_from_file(
            self.current_ota_status_dir / cfg.OTA_STATUS_FNAME, _default=""
        ).upper():
            with contextlib.suppress(KeyError):
                # invalid status string
                return OTAStatus[_status_str]

    # version control

    def _store_standby_version(self, _version: str):
        write_str_to_file_atomic(
            self.standby_ota_status_dir / cfg.OTA_VERSION_FNAME,
            _version,
        )

    # helper methods

    def _is_switching_boot(self, active_slot: str) -> bool:
        """Detect whether we should switch boot or not with ota_status files."""
        # evidence: ota_status
        _is_updating_or_rollbacking = self._load_current_status() in [
            OTAStatus.UPDATING,
            OTAStatus.ROLLBACKING,
        ]

        # evidence: slot_in_use
        _is_switching_slot = self._load_current_slot_in_use() == active_slot

        logger.info(
            f"switch_boot detecting result:"
            f"{_is_updating_or_rollbacking=}, "
            f"{_is_switching_slot=}"
        )
        return _is_updating_or_rollbacking and _is_switching_slot

    # public methods

    # boot control used methods

    def pre_update_current(self):
        """On pre_update stage, set current slot's status to FAILURE
        and set slot_in_use to standby slot."""
        self._store_current_status(OTAStatus.FAILURE)
        self._store_current_slot_in_use(self.standby_slot)

    def pre_update_standby(self, *, version: str):
        """On pre_update stage, set standby slot's status to UPDATING,
        set slot_in_use to standby slot, and set version.

        NOTE: expecting standby slot to be mounted and ready for use!
        """
        # create the ota-status folder unconditionally
        self.standby_ota_status_dir.mkdir(exist_ok=True, parents=True)
        # store status to standby slot
        self._store_standby_status(OTAStatus.UPDATING)
        self._store_standby_version(version)
        self._store_standby_slot_in_use(self.standby_slot)

    def pre_rollback_current(self):
        self._store_current_status(OTAStatus.FAILURE)

    def pre_rollback_standby(self):
        # store ROLLBACKING status to standby
        self.standby_ota_status_dir.mkdir(exist_ok=True, parents=True)
        self._store_standby_status(OTAStatus.ROLLBACKING)

    def load_active_slot_version(self) -> str:
        return read_str_from_file(
            self.current_ota_status_dir / cfg.OTA_VERSION_FNAME,
            _default=cfg.DEFAULT_VERSION_STR,
        )

    def on_failure(self):
        """Store FAILURE to status file on failure."""
        self._store_current_status(OTAStatus.FAILURE)
        # when standby slot is not created, otastatus is not needed to be set
        if self.standby_ota_status_dir.is_dir():
            self._store_standby_status(OTAStatus.FAILURE)

    @property
    def booted_ota_status(self) -> OTAStatus:
        """Loaded current slot's ota_status during boot control starts.

        NOTE: distinguish between the live ota_status maintained by otaclient.

        This property is only meant to be used once when otaclient starts up,
        switch to use live_ota_status by otaclient after otaclient is running.
        """
        return self._ota_status


def cat_proc_cmdline(target: str = "/proc/cmdline") -> str:
    return read_str_from_file(target, _default="")
