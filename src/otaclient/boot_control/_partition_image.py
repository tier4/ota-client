# Copyright 2026 TIER IV, INC. All rights reserved.
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
"""Boot control for a layout whose slots are written as partition images.

Three things make this different from the other boot controllers, and all three come
from the same place — the rootfs is a read-only dm-verity image, so nothing on the
standby slot can be edited:

- **Nothing is mounted.** The standby slot is written as bytes by the DPI, so there
  is no mount point, no fstab to rewrite and no boot files to copy into it.
- **The status files live on the shared boot partition**, not on the slot they
  describe, because the slot has no writable space at all. One directory per slot
  keeps the per-slot meaning that `OTAStatusFilesControl` expects.
- **The boot switch is armed by the DPI during the write**, not here. The boot files
  carry the verity root hash of the rootfs image they were built with, so the pair
  is written together or not at all; arming afterwards from otaclient would be a
  second place that decides what may boot.

What is left for this controller is the part otaclient owns: the OTA status state
machine, and turning "we booted into the slot we staged" into a committed update —
which it asks the DPI to decide, because the commit is gated on the health check.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import NoReturn, Optional, Union

from otaclient import errors as ota_errors
from otaclient._types import FailureType, OTAStatus, VersionDetail
from otaclient.configs import BootloaderType
from otaclient.configs.cfg import cfg
from otaclient_common import cmdhelper

from ._dpi import DPIClient, DPIError, SlotLayout
from ._ota_status_control import OTAStatusFilesControl

logger = logging.getLogger(__name__)

STATE_DIR_BY_PLATFORM = {
    "grub": "/boot",
    "l4t": "/opt/data",
}
"""Where status files that must survive the reboot go, per device side.

The grub layout has a shared boot partition and uses it. A Jetson has no such thing --
its boot chain is in flash and both rootfs slots are overwritten -- so the state goes
on the partition outside both slots that the platform installer mounts there (UDA on
L4T). Getting this wrong writes the standby slot's status into the slot that is about
to be overwritten, so an unknown platform is refused rather than defaulted."""

BOOTLOADER_BY_PLATFORM = {
    "grub": BootloaderType.GRUB_VERITY,
    "l4t": BootloaderType.JETSON_DPI,
}

OTA_STATUS_DNAME = "ota-status"


class PartitionImageBootController:
    """Boot control for the partition-based layout, driving the DPI.

    Implements `BootControllerProtocol`. The slot facts come from the DPI rather than
    from probing here: a slot is a PARTLABEL behind a device-mapper target, and
    getting that wrong means writing the wrong partition, which is unrecoverable.
    """

    def __init__(
        self,
        *,
        dpi: Optional[DPIClient] = None,
        boot_dir: Optional[Union[str, Path]] = None,
    ) -> None:
        self._dpi = dpi if dpi is not None else DPIClient()
        try:
            self._layout: SlotLayout = self._dpi.layout()
        except DPIError as e:
            _err_msg = f"cannot determine the slots: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.BootControlStartupFailed(_err_msg, module=__name__) from e
        logger.info(
            f"partition-image boot control: active={self._layout.active_slot} "
            f"({self._layout.active_dev}), standby={self._layout.standby_slot} "
            f"({self._layout.standby_dev})"
        )

        if boot_dir is None:
            try:
                boot_dir = STATE_DIR_BY_PLATFORM[self._layout.platform]
            except KeyError:
                _err_msg = (
                    f"the DPI reports platform {self._layout.platform!r}, and there is "
                    "no place on it where status about the standby slot is known to "
                    "survive a reboot"
                )
                logger.error(_err_msg)
                raise ota_errors.BootControlStartupFailed(
                    _err_msg, module=__name__
                ) from None
        self._bootloader_type = BOOTLOADER_BY_PLATFORM[self._layout.platform]
        _status_root = Path(boot_dir) / OTA_STATUS_DNAME
        self._switch_concluded = False
        self._writes_slot = True
        self._ota_status_control = OTAStatusFilesControl(
            active_slot=self._layout.active_slot,
            standby_slot=self._layout.standby_slot,
            current_ota_status_dir=_status_root / self._layout.active_slot,
            standby_ota_status_dir=_status_root / self._layout.standby_slot,
            finalize_switching_boot=self._finalize_switching_boot,
        )
        if not self._switch_concluded:
            self._conclude_a_boot_that_did_not_switch()

    def _conclude_a_boot_that_did_not_switch(self) -> None:
        """Let the DPI close a staged update this boot is not the trial boot of.

        A trial boot that did not come up healthy is rebooted back to the committed
        slot by the platform; from the status files that is not a switching boot, so
        the DPI is asked once on every boot (it is idempotent) to close its record.
        """
        self._resume()

    # ------ what the DPI decides ------ #

    def _resume(self) -> bool:
        """Let the DPI conclude whatever it staged before this boot: commit if the
        health check passed, else close the record with the reason, which is stored
        beside the status files for the report after this boot."""
        try:
            _version = self._dpi.resume()
        except DPIError as e:
            logger.error(f"the staged update was not committed: {e!r}")
            with contextlib.suppress(Exception):  # bookkeeping never masks the failure
                self._ota_status_control.store_failure(FailureType.RECOVERABLE, str(e))
            return False
        logger.info(f"the DPI concluded: now running {_version}")
        return True

    def _finalize_switching_boot(self) -> bool:
        """Being on the slot we staged is necessary and not sufficient: the DPI
        commits only once the health check has passed. False is a failed switch; the
        reboot back is the platform's own backstop."""
        self._switch_concluded = True
        return self._resume()

    # ------ properties ------ #

    @property
    def bootloader_type(self) -> BootloaderType:
        return self._bootloader_type

    @property
    def standby_slot_dev(self) -> Path:
        return Path(self._layout.standby_dev)

    def get_standby_slot_dev(self) -> str:
        return self._layout.standby_dev

    def get_standby_slot_path(self) -> Path:
        """Not available on this layout, and not silently either.

        A caller asking for it is a caller about to write files into the standby
        slot, which here holds an image that is verified against a root hash. An
        empty path or a stale mount point would let that write land somewhere else.
        """
        raise NotImplementedError(
            "the standby slot of a partition-based layout is not mounted: it is "
            "written as an image, so there is no path to write files into"
        )

    # ------ versions and status ------ #

    def load_version(self) -> str:
        """What is running: the version otaclient recorded, else the one the image
        itself carries (a device installed by the factory installer or updated by
        another client has no record)."""
        _recorded = self._ota_status_control.load_active_slot_version()
        if _recorded and _recorded != cfg.DEFAULT_VERSION_STR:
            return _recorded
        _from_image = self.load_active_slot_image_version()
        if _from_image:
            logger.info(
                f"no version recorded for {self._layout.active_slot}; the running "
                f"image says {_from_image}"
            )
            return _from_image
        return _recorded

    def load_version_detail(self) -> Optional[VersionDetail]:
        return self._ota_status_control.load_active_slot_version_detail()

    def load_standby_slot_version(self) -> str:
        return self._ota_status_control.load_standby_slot_version()

    def get_booted_ota_status(self) -> OTAStatus:
        return self._ota_status_control.booted_ota_status

    def load_active_slot_image_version(self) -> str:
        """The version the running image itself carries, as the DPI reports it."""
        try:
            return self._dpi.get_version()
        except DPIError as e:
            logger.warning(f"cannot read the running image's version: {e!r}")
            return ""

    # ------ failure paths ------ #

    def on_operation_failure(self) -> None:
        """Nothing to unmount; the record is the whole cleanup. A standby slot the DPI
        already armed is left armed: its one trial boot is health-checked or rolled
        back by the platform, and otaclient does not disarm what it did not arm."""
        logger.warning("on failure: recording it; the standby slot is left as it is")
        self._ota_status_control.on_failure()

    def on_abort(self) -> None:
        logger.info("on abort: persisting ABORTED status to disk")
        self._ota_status_control.on_abort()

    # ------ the update ------ #

    def pre_update(
        self, *, standby_as_ref: bool, erase_standby: bool, writes_slot: bool = True
    ) -> None:
        """Only the status file moves here.

        The standby slot is not prepared, mounted or erased: the image the DPI writes
        covers every byte of it, and the scratch that goes with it is made fresh by
        the same write.

        A payload that writes no slot (data images alone) is tried by rebooting the
        running slot, and the status files have to expect that boot on this slot: a
        slot switch they expected and that never came is a failed update to them.
        """
        del standby_as_ref, erase_standby  # nothing on this layout is built in place
        self._writes_slot = writes_slot
        try:
            logger.info(f"{self.bootloader_type}: pre-update setup...")
            if writes_slot:
                self._ota_status_control.pre_update_current()
            else:
                self._ota_status_control.pre_update_current_same_slot()
        except Exception as e:
            _err_msg = f"failed on pre_update: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.BootControlPreUpdateFailed(
                _err_msg, module=__name__
            ) from e

    def post_update(
        self,
        update_version: str,
        *,
        version_detail: Optional[VersionDetail] = None,
    ) -> None:
        """Record what was written. The boot switch is already armed by then."""
        try:
            logger.info(f"{self.bootloader_type}: post-update setup...")
            if self._writes_slot:
                self._ota_status_control.post_update_standby(
                    version=update_version,
                    version_detail=version_detail,
                )
            else:
                self._ota_status_control.post_update_current_same_slot(
                    version=update_version,
                    version_detail=version_detail,
                )
            logger.info("post update finished, wait for reboot...")
        except Exception as e:
            _err_msg = f"failed on post_update: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.BootControlPostUpdateFailed(
                _err_msg, module=__name__
            ) from e

    def finalizing_update(self, *, chroot: Optional[str] = None) -> NoReturn:
        """Reboot into the slot the DPI armed for one trial boot."""
        logger.info("rebooting into the staged slot...")
        cmdhelper.reboot(chroot=chroot)
