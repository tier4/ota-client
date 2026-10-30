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
from the same place -- the rootfs is a read-only dm-verity image, so nothing on the
standby slot can be edited:

- **Nothing is mounted.** The standby slot is written as bytes by the DPI, so there
  is no mount point, no fstab to rewrite and no boot files to copy into it.
- **The status files live outside the slots**, because a slot has no writable space
  at all: on the shared boot partition (grub) or on the partition the platform
  installer mounts for persistent state (L4T). One directory per slot keeps the
  per-slot meaning that `OTAStatusFilesControl` expects.
- **The boot switch is armed by the DPI during the write**, not here. The boot files
  carry the verity root hash of the rootfs image they were built with, so the pair
  is written together or not at all; arming afterwards from otaclient would be a
  second place that decides what may boot.

What is left for this controller is the part otaclient owns: the OTA status state
machine, and turning "we booted into the slot we staged" into a committed update --
which it asks the DPI to decide, because the commit is gated on the health check.
It is the `PartitionLayoutBootControllerProtocol` the partition-image updater runs
on: the DPI is its installer, and the staging directory is on the persistent storage
of the device side the DPI reports.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from otaclient import errors as ota_errors
from otaclient._types import FailureType, OTAStatus, VersionDetail
from otaclient.configs import BootloaderType
from otaclient.configs.cfg import cfg
from otaclient_common import cmdhelper
from otaclient_common._io import remove_file, write_str_to_file_atomic

from ._dpi import DPIError, DPIProtocol, dpi_from_ecu_info
from ._ota_status_control import OTAStatusFilesControl

if TYPE_CHECKING:
    from ._ota_config_files import OTAConfigFiles

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Platform:
    """Where otaclient keeps its own things on one device side of the contract."""

    bootloader: BootloaderType
    state_dir: str
    """Where status files that must survive the reboot go: the grub layout has a
    shared boot partition and uses it; a Jetson has no such thing -- its boot chain
    is in flash and both rootfs slots are overwritten -- so the state goes on the
    optdata partition the platform installer mounts outside both slots."""
    staging_dir: str
    """Where a payload is staged until the DPI has consumed it. optdata on both
    sides: outside the slots, gigabytes of room (15 GiB on the Jetson layout, where
    the DPI's own download and data image directories are too), mounted at
    /opt/data by the platform, root-owned and not world-writable -- a payload must
    not be staged where another user can place the directory first, nor on the
    scratch, which is 2 GiB and remade on every update."""


PLATFORMS = {
    "grub": _Platform(
        bootloader=BootloaderType.GRUB_VERITY,
        state_dir="/boot",
        staging_dir="/opt/data/otaclient",
    ),
    "l4t": _Platform(
        bootloader=BootloaderType.JETSON_DPI,
        state_dir="/opt/data",
        staging_dir="/opt/data/otaclient",
    ),
}
"""The device sides the DPI may report. Getting the state directory wrong writes
the standby slot's status into the slot that is about to be overwritten, so an
unknown platform is refused rather than defaulted."""

OTA_STATUS_DNAME = "ota-status"

BOOT_ID_FPATH = "/proc/sys/kernel/random/boot_id"
STAGED_IN_BOOT_FNAME = "staged-in-boot"
"""Written beside the status directories when an update starts, holding the boot id
of the boot it started in. The DPI's `resume` concludes "the reboot after a staging"
and consumes its record either way; it must not be asked by an otaclient that merely
restarted before that reboot, while the staged slot is still armed."""


def _current_boot_id() -> str:
    return Path(BOOT_ID_FPATH).read_text().strip()


class PartitionImageBootController:
    """Boot control for the partition-based layout, driving the DPI.

    Implements `BootControllerProtocol` and `PartitionLayoutBootControllerProtocol`.
    The slot facts come from the DPI rather than from probing here: a slot is a
    PARTLABEL behind a device-mapper target, and getting that wrong means writing
    the wrong partition, which is unrecoverable.
    """

    def __init__(
        self,
        *,
        dpi: DPIProtocol | None = None,
        state_dir: str | Path | None = None,
        staging_dir: str | Path | None = None,
    ) -> None:
        try:
            self._dpi = dpi if dpi is not None else dpi_from_ecu_info()
            self._layout = self._dpi.layout()
            logger.info(
                f"partition-image boot control: active={self._layout.active_slot} "
                f"({self._layout.active_dev}), standby={self._layout.standby_slot} "
                f"({self._layout.standby_dev}), platform={self._layout.platform}"
            )
            _platform = PLATFORMS.get(self._layout.platform)
            if _platform is None:
                raise DPIError(
                    f"the DPI reports platform {self._layout.platform!r}, and there "
                    "is no place on it where status about the standby slot is known "
                    "to survive a reboot"
                )
            self._bootloader_type = _platform.bootloader
            self._staging_dir = Path(
                staging_dir if staging_dir is not None else _platform.staging_dir
            )
            self._status_root = (
                Path(state_dir if state_dir is not None else _platform.state_dir)
                / OTA_STATUS_DNAME
            )

            self._switch_concluded = False
            self._not_committed: str | None = None
            self._ota_status_control = OTAStatusFilesControl(
                active_slot=self._layout.active_slot,
                standby_slot=self._layout.standby_slot,
                current_ota_status_dir=self._status_root / self._layout.active_slot,
                standby_ota_status_dir=self._status_root / self._layout.standby_slot,
                finalize_switching_boot=self._finalize_switching_boot,
            )
            if self._switch_concluded:
                # A trial boot the DPI did not commit keeps the mark: the platform
                # takes the ECU back to the committed slot, and the boot there is
                # where the DPI closes its record and says why.
                if self._not_committed is None:
                    self._forget_the_staging_boot()
            elif self._a_reboot_followed_the_staging():
                self._conclude_a_boot_that_did_not_switch()
                self._forget_the_staging_boot()
            self._record_why_the_trial_was_not_committed()
        except ota_errors.OTAError:
            raise
        except Exception as e:
            _layout = getattr(self, "_layout", None)
            _err_msg = (
                "partition-image boot control failed to start (platform "
                f"{_layout.platform if _layout else 'unknown'}, state under "
                f"{getattr(self, '_status_root', 'an undecided directory')}): {e!r}"
            )
            logger.error(_err_msg)
            raise ota_errors.BootControlStartupFailed(_err_msg, module=__name__) from e

    # ------ the boot an update was staged in ------ #

    @property
    def _staged_in_boot_fpath(self) -> Path:
        return self._status_root / STAGED_IN_BOOT_FNAME

    def _remember_the_staging_boot(self) -> None:
        """Before the DPI is asked to stage anything: which boot it happens in."""
        self._status_root.mkdir(parents=True, exist_ok=True)
        write_str_to_file_atomic(self._staged_in_boot_fpath, _current_boot_id())

    def _forget_the_staging_boot(self) -> None:
        remove_file(self._staged_in_boot_fpath)

    def _a_reboot_followed_the_staging(self) -> bool:
        """Did otaclient stage an update in an earlier boot than this one?

        True means the DPI has something to conclude (or to report as not taken).
        False means either nothing was staged by otaclient, or it was staged in
        this very boot and the reboot has not happened yet: the slot is armed, the
        DPI's record is live, and asking `resume` now would consume both.
        """
        try:
            _staged_in = self._staged_in_boot_fpath.read_text().strip()
        except FileNotFoundError:
            return False
        try:
            return _staged_in != _current_boot_id()
        except Exception as e:  # pragma: no cover
            logger.warning(f"cannot read this boot's id, assuming a reboot: {e!r}")
            return True

    def _conclude_a_boot_that_did_not_switch(self) -> None:
        """Let the DPI close a staged update this boot is not the trial boot of.

        A trial boot that did not come up healthy is rebooted back to the committed
        slot by the platform; from the status files that is not a switching boot, so
        the DPI is asked once (it is idempotent) to close its record. That holds
        whether otaclient on the trial boot never got to ask the DPI (a unit that
        failed at once, rebooted before otaclient started) or asked and was refused
        (a health check that ran into its deadline while `resume` waited for it): the
        refusal leaves the record for the slot the platform returns to, and the mark
        for this boot.
        """
        self._resume()

    # ------ what the DPI decides ------ #

    def _resume(self) -> bool:
        """Let the DPI conclude whatever it staged before this boot: commit if the
        health check passed, else close the record with the reason."""
        try:
            _version = self._dpi.resume()
        except DPIError as e:
            logger.error(f"the staged update was not committed: {e!r}")
            self._not_committed = str(e)
            return False
        logger.info(f"the DPI concluded: now running {_version}")
        return True

    def _record_why_the_trial_was_not_committed(self) -> None:
        """A trial boot the DPI did not commit leaves this boot at FAILURE; its reason
        is persisted so that it is reported, not only a bare FAILURE.

        Stored once the status file is settled: the failure info is bound to the
        status file written with it, and the status control rewrites that file
        while it concludes the boot. A reason otaclient already recorded for this
        FAILURE (an update that failed before its reboot) is kept.

        Which failure it is depends on where the ECU is. Back on the slot the update
        switched away from, the platform has rolled the trial back: this slot holds
        the release committed before the update, and the failure is recoverable. On
        the updated slot itself, the commit was refused while the ECU still runs the
        slot on trial, which is a failed switch.
        """
        if self._not_committed is None or (
            self._ota_status_control.booted_ota_status
            not in (OTAStatus.FAILURE, OTAStatus.ROLLBACK_FAILURE)
        ):
            return
        if self._ota_status_control.load_booted_failure_info() is not None:
            return
        _detail = f"the DPI did not commit the trial boot: {self._not_committed}"
        _err: ota_errors.OTAError
        if self._ota_status_control.slot_in_use != self._layout.active_slot:
            _err = ota_errors.BootControlTrialBootFailed(_detail, module=__name__)
        else:
            _err = ota_errors.BootControlSwitchBootFailed(_detail, module=__name__)
        try:
            self._ota_status_control.store_failure_info(
                failure_type=_err.failure_type,
                failure_reason=_err.get_failure_reason(),
            )
        except Exception as e:  # never let bookkeeping stop otaclient from starting
            logger.warning(f"failed to record why the trial boot failed: {e!r}")

    def _finalize_switching_boot(self) -> bool:
        """Being on the slot we staged is necessary and not sufficient: the DPI
        commits only once the health check has passed, and `resume` waits for that
        verdict. False is a failed switch; the reboot back is the platform's own
        backstop."""
        self._switch_concluded = True
        return self._resume()

    # ------ PartitionLayoutBootControllerProtocol ------ #

    @property
    def partition_installer(self) -> DPIProtocol:
        """The DPI writes the payload: it is the installer of this layout."""
        return self._dpi

    @property
    def payload_staging_dir(self) -> Path:
        return self._staging_dir

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

    def load_version_detail(self) -> VersionDetail | None:
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
        back by the platform, and otaclient does not disarm what it did not arm. The
        standby's status record, written before the DPI was asked, makes that trial
        boot a switching boot, so it is concluded and reported like any other."""
        logger.warning("on failure: recording it; the standby slot is left as it is")
        self._ota_status_control.on_failure()

    def on_abort(self) -> None:
        logger.info("on abort: persisting ABORTED status to disk")
        self._ota_status_control.on_abort()

    def store_failure_info(
        self, *, failure_type: FailureType, failure_reason: str
    ) -> None:
        """Persist the failure of the OTA operation to the current slot."""
        self._ota_status_control.store_failure_info(
            failure_type=failure_type, failure_reason=failure_reason
        )

    def get_booted_failure_info(self) -> tuple[FailureType, str] | None:
        """Get the persisted failure info if the booted ota_status is FAILURE."""
        return self._ota_status_control.load_booted_failure_info()

    # ------ the update ------ #

    def pre_update(self, *, standby_as_ref: bool, erase_standby: bool) -> None:
        """Only records move here, and all of them before the DPI touches the slot.

        The standby slot is not prepared, mounted or erased: the image the DPI writes
        covers every byte of it, and the scratch that goes with it is made fresh by
        the same write. What is written is (1) which boot this staging happens in,
        (2) the current slot's "an update is in flight to the standby", and (3) the
        standby's own UPDATING record -- the last one here rather than after the
        write, because the DPI arms the trial boot as part of writing: an abort or a
        power cut after that must still find the staged slot recorded as the one
        being updated, so that its trial boot is concluded instead of booting as
        nothing in particular.
        """
        del standby_as_ref, erase_standby  # nothing on this layout is built in place
        try:
            logger.info(f"{self.bootloader_type}: pre-update setup...")
            self._remember_the_staging_boot()
            self._ota_status_control.pre_update_current()
            self._ota_status_control.post_update_standby(
                version=cfg.DEFAULT_VERSION_STR
            )
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
        version_detail: VersionDetail | None = None,
        ota_config_files: OTAConfigFiles | None = None,
    ) -> None:
        """Record what was written. The boot switch is already armed by then.

        The OTA config files of the image are not installed on this layout: the
        standby slot is never mounted, and what the image ships for /boot/ota is the
        image build's to place.
        """
        if ota_config_files is not None:
            logger.info(
                "the OTA image's ecu_info.yaml / proxy_info.yaml are not installed "
                "on a partition-based layout"
            )
        try:
            logger.info(f"{self.bootloader_type}: post-update setup...")
            self._ota_status_control.post_update_standby(
                version=update_version, version_detail=version_detail
            )
            logger.info("post update finished, wait for reboot...")
        except Exception as e:
            _err_msg = f"failed on post_update: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.BootControlPostUpdateFailed(
                _err_msg, module=__name__
            ) from e

    def finalizing_update(self, *, chroot: str | None = None) -> NoReturn:
        """Reboot into the slot the DPI armed for one trial boot."""
        logger.info("rebooting into the staged slot...")
        cmdhelper.reboot(chroot=chroot)
