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
r"""Shared utils for boot_controller."""


from __future__ import annotations

import contextlib
import logging
import shutil
from pathlib import Path
from typing import Callable, Literal, Optional, Union

from otaclient.app.configs import config as cfg
from otaclient.app.boot_control import _cmdhelper as cmdhelper
from otaclient_api.v2 import types as api_types
from otaclient_common.common import (
    read_str_from_file,
    write_str_to_file_sync,
)

logger = logging.getLogger(__name__)

# fmt: off
PartitionToken = Literal[
    "UUID", "PARTUUID",
    "LABEL", "PARTLABEL",
    "TYPE",
]
# fmt: on


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
        if not _loaded_ota_status:
            logger.info(
                "ota_status files incompleted/not presented, "
                f"initializing and set/store status to {api_types.StatusOta.INITIALIZED.name}..."
            )
            self._store_current_status(api_types.StatusOta.INITIALIZED)
            self._ota_status = api_types.StatusOta.INITIALIZED
            return
        logger.info(f"status loaded from file: {_loaded_ota_status.name}")

        # status except UPDATING and ROLLBACKING(like SUCCESS/FAILURE/ROLLBACK_FAILURE)
        # are remained as it
        if _loaded_ota_status not in [
            api_types.StatusOta.UPDATING,
            api_types.StatusOta.ROLLBACKING,
        ]:
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
                self._ota_status = api_types.StatusOta.SUCCESS
                self._store_current_status(api_types.StatusOta.SUCCESS)
            else:
                self._ota_status = (
                    api_types.StatusOta.ROLLBACK_FAILURE
                    if _loaded_ota_status == api_types.StatusOta.ROLLBACKING
                    else api_types.StatusOta.FAILURE
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
                api_types.StatusOta.ROLLBACK_FAILURE
                if _loaded_ota_status == api_types.StatusOta.ROLLBACKING
                else api_types.StatusOta.FAILURE
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
        if _loaded_slot_in_use and _loaded_slot_in_use != self.active_slot:
            logger.warning(
                f"boot into old slot {self.active_slot}, "
                f"but slot_in_use indicates it should boot into {_loaded_slot_in_use}, "
                "this might indicate a failed finalization at first reboot after update/rollback"
            )

    # slot_in_use control

    def _store_current_slot_in_use(self, _slot: str):
        write_str_to_file_sync(
            self.current_ota_status_dir / cfg.SLOT_IN_USE_FNAME, _slot
        )

    def _store_standby_slot_in_use(self, _slot: str):
        write_str_to_file_sync(
            self.standby_ota_status_dir / cfg.SLOT_IN_USE_FNAME, _slot
        )

    def _load_current_slot_in_use(self) -> Optional[str]:
        if res := read_str_from_file(
            self.current_ota_status_dir / cfg.SLOT_IN_USE_FNAME, default=""
        ):
            return res

    # status control

    def _store_current_status(self, _status: api_types.StatusOta):
        write_str_to_file_sync(
            self.current_ota_status_dir / cfg.OTA_STATUS_FNAME, _status.name
        )

    def _store_standby_status(self, _status: api_types.StatusOta):
        write_str_to_file_sync(
            self.standby_ota_status_dir / cfg.OTA_STATUS_FNAME, _status.name
        )

    def _load_current_status(self) -> Optional[api_types.StatusOta]:
        if _status_str := read_str_from_file(
            self.current_ota_status_dir / cfg.OTA_STATUS_FNAME
        ).upper():
            with contextlib.suppress(KeyError):
                # invalid status string
                return api_types.StatusOta[_status_str]

    # version control

    def _store_standby_version(self, _version: str):
        write_str_to_file_sync(
            self.standby_ota_status_dir / cfg.OTA_VERSION_FNAME,
            _version,
        )

    # helper methods

    def _is_switching_boot(self, active_slot: str) -> bool:
        """Detect whether we should switch boot or not with ota_status files."""
        # evidence: ota_status
        _is_updating_or_rollbacking = self._load_current_status() in [
            api_types.StatusOta.UPDATING,
            api_types.StatusOta.ROLLBACKING,
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
        self._store_current_status(api_types.StatusOta.FAILURE)
        self._store_current_slot_in_use(self.standby_slot)

    def pre_update_standby(self, *, version: str):
        """On pre_update stage, set standby slot's status to UPDATING,
        set slot_in_use to standby slot, and set version.

        NOTE: expecting standby slot to be mounted and ready for use!
        """
        # create the ota-status folder unconditionally
        self.standby_ota_status_dir.mkdir(exist_ok=True, parents=True)
        # store status to standby slot
        self._store_standby_status(api_types.StatusOta.UPDATING)
        self._store_standby_version(version)
        self._store_standby_slot_in_use(self.standby_slot)

    def pre_rollback_current(self):
        self._store_current_status(api_types.StatusOta.FAILURE)

    def pre_rollback_standby(self):
        # store ROLLBACKING status to standby
        self.standby_ota_status_dir.mkdir(exist_ok=True, parents=True)
        self._store_standby_status(api_types.StatusOta.ROLLBACKING)

    def load_active_slot_version(self) -> str:
        return read_str_from_file(
            self.current_ota_status_dir / cfg.OTA_VERSION_FNAME,
            missing_ok=True,
            default=cfg.DEFAULT_VERSION_STR,
        )

    def on_failure(self):
        """Store FAILURE to status file on failure."""
        self._store_current_status(api_types.StatusOta.FAILURE)
        # when standby slot is not created, otastatus is not needed to be set
        if self.standby_ota_status_dir.is_dir():
            self._store_standby_status(api_types.StatusOta.FAILURE)

    @property
    def booted_ota_status(self) -> api_types.StatusOta:
        """Loaded current slot's ota_status during boot control starts.

        NOTE: distinguish between the live ota_status maintained by otaclient.

        This property is only meant to be used once when otaclient starts up,
        switch to use live_ota_status by otaclient after otaclient is running.
        """
        return self._ota_status


class SlotMountHelper:
    """Helper class that provides methods for mounting slots."""

    def __init__(
        self,
        *,
        standby_slot_dev: Union[str, Path],
        standby_slot_mount_point: Union[str, Path],
        active_slot_dev: Union[str, Path],
        active_slot_mount_point: Union[str, Path],
    ) -> None:
        # dev
        self.standby_slot_dev = str(standby_slot_dev)
        self.active_slot_dev = str(active_slot_dev)
        # mount points
        self.standby_slot_mount_point = Path(standby_slot_mount_point)
        self.active_slot_mount_point = Path(active_slot_mount_point)
        self.standby_slot_mount_point.mkdir(exist_ok=True, parents=True)
        self.active_slot_mount_point.mkdir(exist_ok=True, parents=True)
        # standby slot /boot dir
        # NOTE(20230907): this will always be <standby_slot_mp>/boot,
        #                 in the future this attribute will not be used by
        #                 standby slot creater.
        self.standby_boot_dir = self.standby_slot_mount_point / Path(
            cfg.BOOT_DIR
        ).relative_to("/")

    def mount_standby(self) -> None:
        """Mount standby slot dev to <standby_slot_mount_point>."""
        logger.debug("mount standby slot rootfs dev...")
        if cmdhelper.is_target_mounted(self.standby_slot_dev, raise_exception=False):
            logger.debug(f"{self.standby_slot_dev=} is mounted, try to umount it ...")
            cmdhelper.umount(self.standby_slot_dev, raise_exception=False)

        cmdhelper.mount_rw(
            target=self.standby_slot_dev,
            mount_point=self.standby_slot_mount_point,
        )

    def mount_active(self) -> None:
        """Mount active rootfs ready-only."""
        logger.debug("mount active slot rootfs dev...")
        cmdhelper.mount_ro(
            target=self.active_slot_dev,
            mount_point=self.active_slot_mount_point,
        )

    def preserve_ota_folder_to_standby(self):
        """Copy the /boot/ota folder to standby slot to preserve it.

        /boot/ota folder contains the ota setting for this device,
        so we should preserve it for each slot, accross each update.
        """
        logger.debug("copy /boot/ota from active to standby.")
        try:
            _src = self.active_slot_mount_point / Path(cfg.OTA_DIR).relative_to("/")
            _dst = self.standby_slot_mount_point / Path(cfg.OTA_DIR).relative_to("/")
            shutil.copytree(_src, _dst, dirs_exist_ok=True)
        except Exception as e:
            raise ValueError(f"failed to copy /boot/ota from active to standby: {e!r}")

    def prepare_standby_dev(
        self,
        *,
        erase_standby: bool = False,
        fslabel: Optional[str] = None,
    ) -> None:
        cmdhelper.umount(self.standby_slot_dev, raise_exception=False)
        if erase_standby:
            return cmdhelper.mkfs_ext4(self.standby_slot_dev, fslabel=fslabel)

        # TODO: in the future if in-place update mode is implemented, do a
        #   fschck over the standby slot file system.
        if fslabel:
            cmdhelper.set_ext4_fslabel(self.standby_slot_dev, fslabel=fslabel)

    def umount_all(self, *, ignore_error: bool = True):
        logger.debug("unmount standby slot and active slot mount point...")
        cmdhelper.umount(self.standby_slot_mount_point, raise_exception=ignore_error)
        cmdhelper.umount(self.active_slot_mount_point, raise_exception=ignore_error)


def cat_proc_cmdline(target: str = "/proc/cmdline") -> str:
    return read_str_from_file(target, missing_ok=False)
