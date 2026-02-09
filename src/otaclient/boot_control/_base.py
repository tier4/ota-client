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
"""Common base class for boot controllers to reduce code duplication."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import NoReturn

from otaclient import errors as ota_errors
from otaclient._types import OTAStatus

from ._ota_status_control import OTAStatusFilesControl
from ._slot_mnt_helper import SlotMountHelper

logger = logging.getLogger(__name__)


class BootControllerBase(ABC):
    """Abstract base class for boot controllers implementing common patterns.

    This class implements the Template Method pattern to reduce code duplication
    across different boot controller implementations (GRUB, Jetson UEFI, Jetson cboot, RPI).
    Note: This class provides concrete implementations of BootControllerProtocol methods.
    """

    # These must be initialized by subclasses in __init__
    _mp_control: SlotMountHelper
    _ota_status_control: OTAStatusFilesControl

    # ====== Abstract properties that subclasses must implement ======

    @property
    @abstractmethod
    def bootloader_type(self) -> str:
        """The type of bootloader. Must be implemented by subclasses."""
        ...

    # ====== Common property implementations ======

    @property
    def standby_slot_dev(self) -> Path:
        """Return the standby slot device path."""
        return Path(self._mp_control.standby_slot_dev)

    def get_standby_slot_path(self) -> Path:
        """Get the Path points to the standby slot mount point."""
        return self._mp_control.standby_slot_mount_point

    def get_standby_slot_dev(self) -> str:
        """Get the dev to the standby slot."""
        return self._mp_control.standby_slot_dev

    def load_version(self) -> str:
        """Read the version info from the current slot."""
        return self._ota_status_control.load_active_slot_version()

    def load_standby_slot_version(self) -> str:
        """Read the version info from the standby slot."""
        return self._ota_status_control.load_standby_slot_version()

    def get_booted_ota_status(self) -> OTAStatus:
        """Get the ota_status loaded from status file during otaclient starts up."""
        return self._ota_status_control.booted_ota_status

    def get_ota_status_dir(self) -> Path:
        """Get the path to the OTA status directory for the current slot."""
        return self._ota_status_control.current_ota_status_dir

    # ====== Common error handling ======

    def on_operation_failure(self):
        """Cleanup by boot_control implementation when OTA failed."""
        logger.warning("on failure trying to unmount standby slot...")
        self._ota_status_control.on_failure()
        self._mp_control.umount_all(ignore_error=True)

    def on_abort(self):
        """Cleanup by boot_control implementation when OTA is aborted."""
        logger.warning("on abort trying to unmount standby slot...")
        self._ota_status_control.on_abort()
        self._mp_control.umount_all(ignore_error=True)

    # ====== Template Method pattern for update flow ======

    def pre_update(self, *, standby_as_ref: bool, erase_standby: bool):
        """Template method for pre-update setup.

        Common flow:
        1. Update active slot's OTA status
        2. Prepare standby device
        3. Mount standby and active slots
        4. Platform-specific pre-update operations
        """
        try:
            logger.info(f"{self.bootloader_type}: pre-update setup...")

            # Step 1: Update active slot's ota_status
            self._ota_status_control.pre_update_current()

            # Step 2: Prepare standby slot device
            self._pre_update_prepare_standby(erase_standby=erase_standby)

            # Step 3: Mount slots
            self._mp_control.mount_standby()
            self._mp_control.mount_active()

            # Step 4: Platform-specific operations
            self._pre_update_platform_specific(
                standby_as_ref=standby_as_ref, erase_standby=erase_standby
            )

        except Exception as e:
            _err_msg = f"failed on pre_update: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.BootControlPreUpdateFailed(
                _err_msg, module=self.__class__.__module__
            ) from e

    def post_update(self, update_version: str):
        """Template method for post-update setup.

        Common flow:
        1. Update standby slot's OTA status
        2. Platform-specific post-update operations
        3. Unmount all slots
        """
        try:
            logger.info(f"{self.bootloader_type}: post-update setup...")

            # Step 1: Update standby slot's ota-status
            self._ota_status_control.post_update_standby(version=update_version)

            # Step 2: Platform-specific operations (fstab, boot config, firmware, etc.)
            self._post_update_platform_specific(update_version=update_version)

            # Step 3: Prepare to reboot
            self._mp_control.umount_all(ignore_error=True)
            logger.info("post update finished, wait for reboot...")

        except Exception as e:
            _err_msg = f"failed on post_update: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.BootControlPostUpdateFailed(
                _err_msg, module=self.__class__.__module__
            ) from e

    # ====== Platform-specific hook methods ======

    def _pre_update_prepare_standby(self, *, erase_standby: bool):
        """Prepare standby device before mounting.

        Default implementation uses SlotMountHelper.prepare_standby_dev.
        Override if platform needs different behavior (e.g., RPI needs fslabel).
        """
        self._mp_control.prepare_standby_dev(erase_standby=erase_standby)

    def _pre_update_platform_specific(  # noqa: B027
        self, *, standby_as_ref: bool, erase_standby: bool
    ):
        """Platform-specific operations during pre_update (optional hook).

        This hook is called after mounting slots.
        Override to implement platform-specific pre-update logic.

        Default implementation does nothing, which is appropriate for most platforms.
        Example use cases:
        - GRUB: cleanup standby ota_partition folder
        - Jetson cboot: set standby rootfs as unbootable if unified AB is disabled
        """
        pass  # Default: no additional operations (intentionally empty for optional hook)

    @abstractmethod
    def _post_update_platform_specific(self, *, update_version: str):
        """Platform-specific operations during post_update.

        This is where platforms should:
        - Update boot configuration files
        - Update fstab
        - Perform firmware updates
        - Switch boot configuration

        This method is called after updating standby slot's OTA status
        and before unmounting slots.
        """
        ...

    @abstractmethod
    def finalizing_update(self, *, chroot: str | None = None) -> NoReturn:
        """Finalize update and reboot.

        Normally this method only reboots the device.
        Some platforms may need custom reboot logic.
        """
        ...
