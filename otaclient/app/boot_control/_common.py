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
import shutil
from pathlib import Path
from typing import Optional, Callable

from otaclient._utils.path import replace_root
from otaclient._utils.typing import StrOrPath

from .. import log_setting
from ..configs import config as cfg
from ..common import read_str_from_file, write_str_to_file_sync
from ..proto import wrapper

from ._cmdhelpers import (
    is_target_mounted,
    mkfs_ext4,
    umount,
    mount_rw,
    mount_ro,
    set_ext4_dev_fslabel,
    SubProcessCalledFailed,
    MountError,
)


logger = log_setting.get_logger(__name__)

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
        current_ota_status_dir: StrOrPath,
        standby_ota_status_dir: StrOrPath,
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
                f"initializing and set/store status to {wrapper.StatusOta.INITIALIZED.name}..."
            )
            self._store_current_status(wrapper.StatusOta.INITIALIZED)
            self._ota_status = wrapper.StatusOta.INITIALIZED
            return
        logger.info(f"status loaded from file: {_loaded_ota_status.name}")

        # status except UPDATING and ROLLBACKING(like SUCCESS/FAILURE/ROLLBACK_FAILURE)
        # are remained as it
        if _loaded_ota_status not in [
            wrapper.StatusOta.UPDATING,
            wrapper.StatusOta.ROLLBACKING,
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
                self._ota_status = wrapper.StatusOta.SUCCESS
                self._store_current_status(wrapper.StatusOta.SUCCESS)
            else:
                self._ota_status = (
                    wrapper.StatusOta.ROLLBACK_FAILURE
                    if _loaded_ota_status == wrapper.StatusOta.ROLLBACKING
                    else wrapper.StatusOta.FAILURE
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
                wrapper.StatusOta.ROLLBACK_FAILURE
                if _loaded_ota_status == wrapper.StatusOta.ROLLBACKING
                else wrapper.StatusOta.FAILURE
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

    def _store_current_status(self, _status: wrapper.StatusOta):
        write_str_to_file_sync(
            self.current_ota_status_dir / cfg.OTA_STATUS_FNAME, _status.name
        )

    def _store_standby_status(self, _status: wrapper.StatusOta):
        write_str_to_file_sync(
            self.standby_ota_status_dir / cfg.OTA_STATUS_FNAME, _status.name
        )

    def _load_current_status(self) -> Optional[wrapper.StatusOta]:
        if _status_str := read_str_from_file(
            self.current_ota_status_dir / cfg.OTA_STATUS_FNAME
        ).upper():
            try:
                return wrapper.StatusOta[_status_str]
            except KeyError:
                pass  # invalid status string

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
            wrapper.StatusOta.UPDATING,
            wrapper.StatusOta.ROLLBACKING,
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
        self._store_current_status(wrapper.StatusOta.FAILURE)
        self._store_current_slot_in_use(self.standby_slot)

    def pre_update_standby(self, *, version: str):
        """On pre_update stage, set standby slot's status to UPDATING,
        set slot_in_use to standby slot, and set version.

        NOTE: expecting standby slot to be mounted and ready for use!
        """
        # create the ota-status folder unconditionally
        self.standby_ota_status_dir.mkdir(exist_ok=True, parents=True)
        # store status to standby slot
        self._store_standby_status(wrapper.StatusOta.UPDATING)
        self._store_standby_version(version)
        self._store_standby_slot_in_use(self.standby_slot)

    def pre_rollback_current(self):
        self._store_current_status(wrapper.StatusOta.FAILURE)

    def pre_rollback_standby(self):
        # store ROLLBACKING status to standby
        self.standby_ota_status_dir.mkdir(exist_ok=True, parents=True)
        self._store_standby_status(wrapper.StatusOta.ROLLBACKING)

    def load_active_slot_version(self) -> str:
        return read_str_from_file(
            self.current_ota_status_dir / cfg.OTA_VERSION_FNAME,
            missing_ok=True,
            default=cfg.DEFAULT_VERSION_STR,
        )

    def on_failure(self):
        """Store FAILURE to status file on failure."""
        self._store_current_status(wrapper.StatusOta.FAILURE)
        # when standby slot is not created, otastatus is not needed to be set
        if self.standby_ota_status_dir.is_dir():
            self._store_standby_status(wrapper.StatusOta.FAILURE)

    @property
    def booted_ota_status(self) -> wrapper.StatusOta:
        """Loaded current slot's ota_status during boot control starts.

        NOTE: distinguish between the live ota_status maintained by otaclient.

        This property is only meant to be used once when otaclient starts up,
        switch to use live_ota_status by otaclient after otaclient is running.
        """
        return self._ota_status


class SlotMountException(Exception):
    """Exception type used by SlotMountHelper."""


class SlotMountHelper:
    """Helper class that provides methods for mounting slots."""

    def __init__(
        self,
        *,
        standby_slot_dev: StrOrPath,
        standby_slot_mount_point: StrOrPath,
        active_slot_dev: StrOrPath,
        active_slot_mount_point: StrOrPath,
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
        self.standby_boot_dir = self.standby_slot_mount_point / "boot"

    def mount_standby_slot_dev(self) -> None:
        """Mount standby slot dev r/w to <standby_slot_mount_point>.

        Raises:
            SlotMountException on failed mount.
        """
        logger.debug("mount standby slot rootfs dev...")
        try:
            mount_rw(
                target=self.standby_slot_dev,
                mount_point=self.standby_slot_mount_point,
                raise_exception=True,
            )
        except MountError as e:
            _err_msg = f"failed to mount {self.standby_slot_dev=} to {self.standby_slot_mount_point=}: {e!r}"
            logger.error(_err_msg)
            raise SlotMountException(_err_msg) from e

    def mount_active_slot_dev(self) -> None:
        """Mount active rootfs ready-only.

        Raises:
            SlotMountException on failed mount.
        """
        logger.debug("mount active slot rootfs dev...")
        try:
            mount_ro(
                target=self.active_slot_dev,
                mount_point=self.active_slot_mount_point,
                raise_exception=True,
            )
        except MountError as e:
            _err_msg = f"failed to mount {self.active_slot_dev=} to {self.standby_slot_mount_point}: {e!r}"
            logger.error(_err_msg)
            raise SlotMountException(_err_msg) from e

    def preserve_ota_folder_to_standby(self):
        """Copy the /boot/ota folder to standby slot to preserve it.

        /boot/ota folder contains the ota setting for this device,
        so we should preserve it for each slot, accross each update.
        """
        logger.debug("copy /boot/ota from active to standby.")

        _src = Path(cfg.BOOT_OTA_DPATH)
        _dst = Path(
            replace_root(cfg.BOOT_OTA_DPATH, cfg.ACTIVE_ROOTFS, cfg.STANDBY_SLOT_MP)
        )
        try:
            shutil.copytree(_src, _dst, dirs_exist_ok=True)
        except Exception as e:
            raise ValueError(f"failed to copy {_src=} to {_dst=}: {e!r}")

    def umount_all(self):
        """Umount all mount points and ignore all errors."""
        logger.debug("unmount standby slot and active slot mount point...")
        try:
            umount(
                self.standby_slot_mount_point,
                force=True,
                lazy=False,
                recursive=True,
                raise_exception=False,
            )
        except Exception:
            logger.warning(f"failed to umount {self.standby_slot_mount_point=}")

        try:
            umount(
                self.active_slot_mount_point,
                force=True,
                lazy=False,
                recursive=True,
                raise_exception=False,
            )
        except Exception:
            logger.warning(f"failed to umount {self.active_slot_mount_point=}")


def cat_proc_cmdline(target: str = "/proc/cmdline") -> str:
    return read_str_from_file(target, missing_ok=False)


def prepare_standby_slot_dev_ext4(
    standby_slot_dev: StrOrPath,
    *,
    erase_standby: bool,
    fslabel: Optional[str] = None,
    fsuuid: Optional[str] = None,
) -> None:
    """Prepare the standby slot dev's filesystem as ext4.

    NOTE: if erase_standby=False, assuming that the target partition is valid ext4 partition,
          the caller should run e2fsck before calling this function!

    Args:
        fslabel(str = None): set the partition fslabel to <fslabel>.
            if <erase_standby> is True, <fslabel> will be passed to mkfs.ext4 command,
            if <erase_standby> is False, <fslabel> will be set with e2label.
        fsuuid(str = None): (only used when <erase_standby> is True) set the newly formatted
            partition's fsuuid to <fsuuid>.

    Raises:
        Passthrough the SubProcessCalledFailed raised by corresponding
            command execution.
    """
    # try umount the dev if it is mounted somewhere
    if is_target_mounted(standby_slot_dev, raise_exception=False):
        try:
            umount(
                standby_slot_dev,
                force=True,
                lazy=False,
                recursive=True,
                raise_exception=True,
            )
        except SubProcessCalledFailed:
            logger.warning(f"{standby_slot_dev} is mounted and failed to umount it")

    if erase_standby:
        return mkfs_ext4(standby_slot_dev, fslabel=fslabel, fsuuid=fsuuid)

    if fslabel:
        # TODO: check the standby file system status somewhere else
        #       if not erase the standby slot.
        set_ext4_dev_fslabel(standby_slot_dev, fslabel=fslabel)
