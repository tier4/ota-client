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
"""Boot control for the partition-image layout.

The DPI is stubbed here: what these tests are about is the division of labour
between it and otaclient — who decides the slots, who arms the boot switch, and what
otaclient records — not the DPI's own behaviour, which is tested where it lives.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import pytest

from otaclient import errors as ota_errors
from otaclient._types import OTAStatus
from otaclient.boot_control._dpi import DPIError, SlotLayout
from otaclient.boot_control._partition_image import (
    OTA_STATUS_DNAME,
    STATE_DIR_BY_PLATFORM,
    PartitionImageBootController,
)
from otaclient.configs import BootloaderType

LAYOUT = SlotLayout(
    active_slot="rootfs_a",
    standby_slot="rootfs_b",
    active_dev="/dev/sda3",
    standby_dev="/dev/sda4",
)


class FakeDPI:
    """Records what otaclient asked of the DPI, and answers as the real one would."""

    def __init__(
        self,
        *,
        layout: Optional[SlotLayout] = LAYOUT,
        resume_version: Optional[str] = "2.9.0",
        resume_error: bool = False,
        version: str = "2.9.0",
    ) -> None:
        self._layout = layout
        self._resume_version = resume_version
        self._resume_error = resume_error
        self._version = version
        self.calls: List[str] = []

    def layout(self) -> SlotLayout:
        self.calls.append("layout")
        if self._layout is None:
            raise DPIError("unrecognised layout")
        return self._layout

    def get_version(self, *, name: Optional[str] = None) -> str:
        self.calls.append("get-version")
        return self._version

    def resume(self) -> Optional[str]:
        self.calls.append("resume")
        if self._resume_error:
            raise DPIError("the health check did not pass")
        return self._resume_version


def status_dir(boot_dir: Path, slot: str) -> Path:
    return boot_dir / OTA_STATUS_DNAME / slot


def make_controller(boot_dir: Path, dpi: Optional[FakeDPI] = None):
    return PartitionImageBootController(dpi=dpi or FakeDPI(), boot_dir=boot_dir)


class TestStartup:
    def test_the_slots_come_from_the_dpi(self, tmp_path: Path):
        """otaclient does not probe for them: a slot is a PARTLABEL behind a
        device-mapper target, and writing the wrong partition is unrecoverable."""
        _dpi = FakeDPI()
        _ctrl = make_controller(tmp_path, _dpi)

        assert _dpi.calls == ["layout", "resume"]
        assert _ctrl.get_standby_slot_dev() == "/dev/sda4"
        assert _ctrl.standby_slot_dev == Path("/dev/sda4")
        assert _ctrl.bootloader_type == BootloaderType.GRUB_VERITY

    def test_status_files_live_on_the_shared_boot_partition(self, tmp_path: Path):
        """The slot they describe is a read-only verity image with nowhere to write,
        so they go beside the boot files, one directory per slot."""
        make_controller(tmp_path)
        assert (status_dir(tmp_path, "rootfs_a") / "status").is_file()

    def test_an_unrecognised_layout_stops_startup(self, tmp_path: Path):
        with pytest.raises(ota_errors.BootControlStartupFailed):
            make_controller(tmp_path, FakeDPI(layout=None))

    def test_the_standby_slot_has_no_mount_point(self, tmp_path: Path):
        """Asking for one means being about to write files into an image; that must
        fail loudly rather than land somewhere else."""
        with pytest.raises(NotImplementedError, match="written as an image"):
            make_controller(tmp_path).get_standby_slot_path()


class TestSwitchingBoot:
    """The boot after an update was staged: otaclient asks the DPI to conclude it."""

    @staticmethod
    def stage_an_update(boot_dir: Path, *, booted_into: str) -> None:
        """What post_update left behind, seen from the slot we just booted into."""
        _dir = status_dir(boot_dir, booted_into)
        _dir.mkdir(parents=True, exist_ok=True)
        (_dir / "status").write_text(OTAStatus.UPDATING.name)
        (_dir / "slot_in_use").write_text(booted_into)

    def test_a_healthy_trial_boot_is_committed(self, tmp_path: Path):
        self.stage_an_update(tmp_path, booted_into="rootfs_a")
        _dpi = FakeDPI()

        _ctrl = make_controller(tmp_path, _dpi)

        assert "resume" in _dpi.calls
        assert _ctrl.get_booted_ota_status() == OTAStatus.SUCCESS

    def test_a_slot_that_came_up_unhealthy_is_a_failure(self, tmp_path: Path):
        """Being on the staged slot is necessary, not sufficient: the DPI commits
        only once the health check has passed, and its refusal is the verdict."""
        self.stage_an_update(tmp_path, booted_into="rootfs_a")
        _dpi = FakeDPI(resume_error=True)

        _ctrl = make_controller(tmp_path, _dpi)

        assert "resume" in _dpi.calls
        assert _ctrl.get_booted_ota_status() == OTAStatus.FAILURE

    def test_a_rolled_back_boot_still_lets_the_dpi_close_its_record(
        self, tmp_path: Path
    ):
        """A trial boot that did not come up healthy is rebooted back by the platform,
        and otaclient wakes on the old slot — where the status files say this is no
        switching boot. The DPI still holds the staged record and the reason, so it is
        asked once anyway; otherwise the update stays staged for ever."""
        _dir = status_dir(tmp_path, "rootfs_a")
        _dir.mkdir(parents=True)
        (_dir / "status").write_text(OTAStatus.FAILURE.name)
        (_dir / "slot_in_use").write_text("rootfs_b")  # what pre_update wrote
        _dpi = FakeDPI(resume_error=True)  # the DPI reports the attempt failed

        _ctrl = make_controller(tmp_path, _dpi)

        assert _dpi.calls == ["layout", "resume"]
        assert _ctrl.get_booted_ota_status() == OTAStatus.FAILURE

    def test_an_ordinary_boot_does_not_ask_the_dpi_to_conclude_anything(
        self, tmp_path: Path
    ):
        _dir = status_dir(tmp_path, "rootfs_a")
        _dir.mkdir(parents=True)
        (_dir / "status").write_text(OTAStatus.SUCCESS.name)
        (_dir / "slot_in_use").write_text("rootfs_a")
        _dpi = FakeDPI()

        _ctrl = make_controller(tmp_path, _dpi)

        # asked once, because only the DPI knows whether anything is staged; it
        # answers that nothing is, and no commit happens
        assert _dpi.calls == ["layout", "resume"]
        assert _ctrl.get_booted_ota_status() == OTAStatus.SUCCESS


class TestUpdateFlow:
    def test_pre_update_only_records(self, tmp_path: Path):
        """Nothing is mounted or erased: the image the DPI writes covers every byte
        of the slot, and the scratch that goes with it is made by the same write."""
        _dpi = FakeDPI()
        _ctrl = make_controller(tmp_path, _dpi)

        _ctrl.pre_update(standby_as_ref=False, erase_standby=True)

        assert _dpi.calls == ["layout", "resume"]  # no mkfs, no mount, no arming
        assert (
            status_dir(tmp_path, "rootfs_a") / "slot_in_use"
        ).read_text() == "rootfs_b"

    def test_post_update_records_what_was_written(self, tmp_path: Path):
        _ctrl = make_controller(tmp_path)

        _ctrl.post_update("2.9.0")

        _standby = status_dir(tmp_path, "rootfs_b")
        assert (_standby / "status").read_text() == OTAStatus.UPDATING.name
        assert (_standby / "version").read_text() == "2.9.0"
        assert (_standby / "slot_in_use").read_text() == "rootfs_b"

    def test_a_failure_leaves_the_armed_slot_alone(self, tmp_path: Path):
        """The armed slot gets one trial boot and is rolled back if it fails, which
        is the design's own safe path; otaclient does not disarm what it did not arm."""
        _dpi = FakeDPI()
        _ctrl = make_controller(tmp_path, _dpi)

        _ctrl.on_operation_failure()

        assert _dpi.calls == ["layout", "resume"]
        assert (status_dir(tmp_path, "rootfs_a") / "status").read_text() == (
            OTAStatus.FAILURE.name
        )


def test_the_version_reported_falls_back_to_the_running_image(tmp_path: Path):
    """A device the factory installer wrote has no version recorded by otaclient, and
    reporting nothing reads as "nothing is installed" rather than "installed by
    someone else"."""
    _dpi = FakeDPI(version="2.9.0")
    _ctrl = make_controller(tmp_path, _dpi)

    assert _ctrl.load_version() == "2.9.0"


def test_a_recorded_version_wins_over_the_images_own(tmp_path: Path):
    """Once otaclient has installed something here, that is the answer: the image's
    own version cannot know about a payload delivered on top of it."""
    _dir = status_dir(tmp_path, "rootfs_a")
    _dir.mkdir(parents=True)
    (_dir / "version").write_text("3.0.0")
    _ctrl = make_controller(tmp_path, FakeDPI(version="2.9.0"))

    assert _ctrl.load_version() == "3.0.0"


def test_the_running_images_own_version_comes_from_the_dpi(tmp_path: Path):
    """The status files say what otaclient installed; this says what is running,
    which is the answer on a device the factory installer wrote."""
    _dpi = FakeDPI(version="2.9.0")
    assert make_controller(tmp_path, _dpi).load_active_slot_image_version() == "2.9.0"
    assert "get-version" in _dpi.calls


class TestWhereTheStatusGoes:
    """The status files must outlive the reboot, and where that is differs per device
    side. Written into the slot that is about to be overwritten, they are gone exactly
    when they are needed, so the platform the DPI reports decides -- otaclient does
    not guess, and does not default."""

    @staticmethod
    def jetson_layout(platform: str = "l4t") -> SlotLayout:
        return SlotLayout(
            active_slot="APP",
            standby_slot="APP_b",
            active_dev="/dev/mmcblk0p1",
            standby_dev="/dev/mmcblk0p2",
            platform=platform,
        )

    def test_the_real_paths_are_the_ones_each_platform_installs(self):
        """X2-Gen2 has a shared boot partition mounted rw on every boot. L4T has no
        such thing -- its boot chain is in flash and both rootfs slots are
        overwritten -- so the state goes on UDA, which `deploy/l4t/install-platform.sh`
        mounts at /opt/data."""
        assert STATE_DIR_BY_PLATFORM["grub"] == "/boot"
        assert STATE_DIR_BY_PLATFORM["l4t"] == "/opt/data"

    def test_a_jetson_writes_its_status_where_that_platform_keeps_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _state = tmp_path / "opt-data"
        monkeypatch.setitem(STATE_DIR_BY_PLATFORM, "l4t", str(_state))

        _ctrl = PartitionImageBootController(dpi=FakeDPI(layout=self.jetson_layout()))

        assert _ctrl.bootloader_type == BootloaderType.JETSON_DPI
        assert status_dir(_state, "APP").is_dir()

    def test_a_platform_with_no_known_place_for_it_is_refused(self):
        """Rather than defaulting to /boot, which on that device may be inside the
        slot the update is about to overwrite."""
        with pytest.raises(
            ota_errors.BootControlStartupFailed, match="survive a reboot"
        ):
            PartitionImageBootController(
                dpi=FakeDPI(layout=self.jetson_layout(platform="driveos"))
            )

    def test_x2gen2_is_still_what_a_dpi_that_names_no_platform_means(
        self, tmp_path: Path
    ):
        """The layout JSON grew the field; a DPI from before it reads as X2-Gen2."""
        _ctrl = make_controller(tmp_path)
        assert _ctrl.bootloader_type == BootloaderType.GRUB_VERITY
        assert status_dir(tmp_path, "rootfs_a").is_dir()
