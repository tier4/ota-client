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
between it and otaclient -- who decides the slots, who arms the boot switch, and what
otaclient records -- not the DPI's own behaviour, which is tested where it lives.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from otaclient import errors as ota_errors
from otaclient._types import FailureType, OTAStatus
from otaclient.boot_control import _partition_image, get_boot_controller
from otaclient.boot_control._dpi import DPIError, SlotLayout
from otaclient.boot_control._partition_image import (
    OTA_STATUS_DNAME,
    PLATFORMS,
    STAGED_IN_BOOT_FNAME,
    PartitionImageBootController,
)
from otaclient.boot_control.protocol import (
    BootControllerProtocol,
    PartitionLayoutBootControllerProtocol,
)
from otaclient.configs import BootloaderType

LAYOUT = SlotLayout(
    active_slot="rootfs_a",
    standby_slot="rootfs_b",
    active_dev="/dev/sda3",
    standby_dev="/dev/sda4",
    platform="grub",
)
LAYOUT_ON_B = SlotLayout(
    active_slot="rootfs_b",
    standby_slot="rootfs_a",
    active_dev="/dev/sda4",
    standby_dev="/dev/sda3",
    platform="grub",
)
"""The same device, booted into the other slot."""


class FakeDPI:
    """Records what otaclient asked of the DPI, and answers as the real one would."""

    def __init__(
        self,
        *,
        layout: SlotLayout | None = LAYOUT,
        resume_version: str | None = "2.9.0",
        resume_error: str | None = None,
        version: str = "2.9.0",
        version_error: str | None = None,
    ) -> None:
        self._layout = layout
        self._resume_version = resume_version
        self._resume_error = resume_error
        self._version = version
        self._version_error = version_error
        self.calls: list[str] = []

    def layout(self) -> SlotLayout:
        self.calls.append("layout")
        if self._layout is None:
            raise DPIError("unrecognised layout")
        return self._layout

    def get_version(self, *, name: str | None = None) -> str:
        self.calls.append("get-version")
        if self._version_error is not None:
            raise DPIError(self._version_error)
        return self._version

    def resume(self) -> str | None:
        self.calls.append("resume")
        if self._resume_error is not None:
            raise DPIError(self._resume_error)
        return self._resume_version

    # the installer side, never called by the boot controller itself
    def source_digest(self, *, size: int, data_image: str | None = None) -> str:
        raise AssertionError("the boot controller does not ask for digests")

    def install(self, **kwargs) -> None:
        raise AssertionError("the boot controller does not install")


def status_dir(state_dir: Path, slot: str) -> Path:
    return state_dir / OTA_STATUS_DNAME / slot


def staged_in_boot(state_dir: Path) -> Path:
    return state_dir / OTA_STATUS_DNAME / STAGED_IN_BOOT_FNAME


def make_controller(state_dir: Path, dpi: FakeDPI | None = None):
    return PartitionImageBootController(dpi=dpi or FakeDPI(), state_dir=state_dir)


@pytest.fixture
def this_boot(monkeypatch: pytest.MonkeyPatch):
    """The boot id of the boot a test runs in, settable: `this_boot("boot-2")`."""
    _ids = {"now": "boot-1"}
    monkeypatch.setattr(_partition_image, "_current_boot_id", lambda: _ids["now"])

    def _set(boot_id: str) -> None:
        _ids["now"] = boot_id

    return _set


def an_update_was_staged_in(state_dir: Path, boot_id: str) -> None:
    """What pre_update leaves beside the status directories."""
    (state_dir / OTA_STATUS_DNAME).mkdir(parents=True, exist_ok=True)
    staged_in_boot(state_dir).write_text(boot_id)


class TestStartup:
    def test_the_slots_come_from_the_dpi(self, tmp_path: Path):
        """otaclient does not probe for them: a slot is a PARTLABEL behind a
        device-mapper target, and writing the wrong partition is unrecoverable."""
        _dpi = FakeDPI()
        _ctrl = make_controller(tmp_path, _dpi)

        assert _dpi.calls == ["layout"]
        assert _ctrl.get_standby_slot_dev() == "/dev/sda4"
        assert _ctrl.standby_slot_dev == Path("/dev/sda4")
        assert _ctrl.bootloader_type == BootloaderType.GRUB_VERITY

    def test_it_is_the_boot_controller_the_partition_updater_runs_on(
        self, tmp_path: Path
    ):
        """OTAClient picks the partition-image updater by this protocol, and gets the
        DPI as the installer and a staging directory on persistent storage from it."""
        _dpi = FakeDPI()
        _ctrl = make_controller(tmp_path, _dpi)

        _as_boot_control: BootControllerProtocol = _ctrl  # the type checker's share
        assert _as_boot_control.bootloader_type == BootloaderType.GRUB_VERITY
        assert isinstance(_ctrl, PartitionLayoutBootControllerProtocol)
        assert _ctrl.partition_installer is _dpi
        assert _ctrl.payload_staging_dir == Path(PLATFORMS["grub"].staging_dir)

    def test_the_selecter_knows_both_bootloader_types(self):
        assert (
            get_boot_controller(BootloaderType.GRUB_VERITY)
            is PartitionImageBootController
        )
        assert (
            get_boot_controller(BootloaderType.JETSON_DPI)
            is PartitionImageBootController
        )

    def test_status_files_live_outside_the_slots(self, tmp_path: Path):
        """The slot they describe is a read-only verity image with nowhere to write,
        so they go on the shared boot partition, one directory per slot."""
        make_controller(tmp_path)
        assert (status_dir(tmp_path, "rootfs_a") / "status").is_file()

    def test_an_unrecognised_layout_stops_startup(self, tmp_path: Path):
        with pytest.raises(ota_errors.BootControlStartupFailed):
            make_controller(tmp_path, FakeDPI(layout=None))

    def test_a_state_directory_that_cannot_be_made_names_its_place(
        self, tmp_path: Path
    ):
        """optdata not mounted, a read-only root: the failure says where it tried."""
        _not_a_dir = tmp_path / "file"
        _not_a_dir.write_text("")
        with pytest.raises(
            ota_errors.BootControlStartupFailed, match=r"platform grub, state under"
        ):
            make_controller(_not_a_dir)

    def test_the_standby_slot_has_no_mount_point(self, tmp_path: Path):
        """Asking for one means being about to write files into an image; that must
        fail loudly rather than land somewhere else."""
        with pytest.raises(NotImplementedError, match="written as an image"):
            make_controller(tmp_path).get_standby_slot_path()


class TestSwitchingBoot:
    """The boot after an update was staged: otaclient asks the DPI to conclude it."""

    @staticmethod
    def stage_an_update(state_dir: Path, *, booted_into: str) -> None:
        """What pre_update / post_update left behind, seen from the slot we just
        booted into."""
        _dir = status_dir(state_dir, booted_into)
        _dir.mkdir(parents=True, exist_ok=True)
        (_dir / "status").write_text(OTAStatus.UPDATING.name)
        (_dir / "slot_in_use").write_text(booted_into)
        an_update_was_staged_in(state_dir, "boot-0")

    def test_a_healthy_trial_boot_is_committed(self, tmp_path: Path, this_boot):
        self.stage_an_update(tmp_path, booted_into="rootfs_a")
        _dpi = FakeDPI()

        _ctrl = make_controller(tmp_path, _dpi)

        assert _dpi.calls == ["layout", "resume"]
        assert _ctrl.get_booted_ota_status() == OTAStatus.SUCCESS
        assert not staged_in_boot(tmp_path).exists(), "concluded, nothing to ask again"

    def test_a_slot_that_came_up_unhealthy_is_a_failure(
        self, tmp_path: Path, this_boot
    ):
        """Being on the staged slot is necessary, not sufficient: the DPI commits
        only once the health check has passed, and its refusal is the verdict."""
        self.stage_an_update(tmp_path, booted_into="rootfs_a")
        _dpi = FakeDPI(resume_error="the health check did not pass")

        _ctrl = make_controller(tmp_path, _dpi)

        assert "resume" in _dpi.calls
        assert _ctrl.get_booted_ota_status() == OTAStatus.FAILURE
        _info = _ctrl.get_booted_failure_info()
        assert _info is not None, "reported with its reason, not a bare FAILURE"
        assert _info[0] == FailureType.UNRECOVERABLE, "still running the slot on trial"
        assert _info[1].startswith("E557")
        assert "the health check did not pass" in _info[1]
        assert staged_in_boot(tmp_path).exists(), "kept for the boot that follows"

    def test_a_rolled_back_boot_still_lets_the_dpi_close_its_record(
        self, tmp_path: Path, this_boot
    ):
        """A trial boot that did not come up healthy is rebooted back by the platform,
        and otaclient wakes on the old slot -- where the status files say this is no
        switching boot. The DPI still holds the staged record and the reason, so it is
        asked once anyway; otherwise the update stays staged for ever."""
        _dir = status_dir(tmp_path, "rootfs_a")
        _dir.mkdir(parents=True)
        (_dir / "status").write_text(OTAStatus.FAILURE.name)
        (_dir / "slot_in_use").write_text("rootfs_b")  # what pre_update wrote
        an_update_was_staged_in(tmp_path, "boot-0")  # and this, before the reboot
        _dpi = FakeDPI(
            resume_error="staged 1.7.2 on rootfs_b but booted rootfs_a; "
            "the update did not take: failed: fhs-fail.service"
        )

        _ctrl = make_controller(tmp_path, _dpi)

        assert _dpi.calls == ["layout", "resume"]
        assert _ctrl.get_booted_ota_status() == OTAStatus.FAILURE
        _info = _ctrl.get_booted_failure_info()
        assert _info is not None
        # back on the release committed before the update: nothing to repair
        assert _info[0] == FailureType.RECOVERABLE
        assert _info[1].startswith("E560")
        assert "fhs-fail.service" in _info[1]
        assert not staged_in_boot(tmp_path).exists()

    def test_a_trial_refused_on_its_own_boot_is_reported_from_the_slot_it_returns_to(
        self, tmp_path: Path, this_boot
    ):
        """A health check that runs into its deadline does so while otaclient on the
        trial boot waits in `resume`: the DPI refuses the commit there, leaves the
        reason in its record and reboots, and the platform boots the old slot. That
        boot still lets the DPI close the record, and reports the rollback with the
        reason the trial boot left -- not a bare FAILURE, and not a failed switch."""
        _staging = make_controller(tmp_path)
        _staging.pre_update(standby_as_ref=False, erase_standby=True)
        _staging.post_update("3.0.0")

        this_boot("boot-2")  # the trial boot of rootfs_b
        _on_trial = FakeDPI(
            layout=LAYOUT_ON_B,
            resume_error="health check did not pass on rootfs_b: "
            "failed: autoware.service",
        )
        _trial = make_controller(tmp_path, _on_trial)
        assert _on_trial.calls == ["layout", "resume"]
        assert _trial.get_booted_ota_status() == OTAStatus.FAILURE
        assert staged_in_boot(tmp_path).read_text() == "boot-1"

        this_boot("boot-3")  # the platform's reboot back into rootfs_a
        _on_return = FakeDPI(
            resume_error="staged 3.0.0 on rootfs_b but booted rootfs_a; the update "
            "did not take: health check on rootfs_b: failed: autoware.service"
        )
        _returned = make_controller(tmp_path, _on_return)

        assert _on_return.calls == ["layout", "resume"]
        assert _returned.get_booted_ota_status() == OTAStatus.FAILURE
        _info = _returned.get_booted_failure_info()
        assert _info is not None
        assert _info[0] == FailureType.RECOVERABLE
        assert _info[1].startswith("E560")
        assert "autoware.service" in _info[1]
        assert not staged_in_boot(tmp_path).exists()

    def test_a_refused_slot_the_ecu_stays_on_is_asked_once_more_and_kept_failed(
        self, tmp_path: Path, this_boot
    ):
        """Where the platform does not take the ECU back (the DPI committed the slot
        and then could not promote a data image, say), the next boot is on the same
        slot: the DPI has nothing left to conclude, the mark goes, and the failed
        switch stays what is reported."""
        _staging = make_controller(tmp_path)
        _staging.pre_update(standby_as_ref=False, erase_standby=True)
        _staging.post_update("3.0.0")
        this_boot("boot-2")
        make_controller(
            tmp_path,
            FakeDPI(
                layout=LAYOUT_ON_B,
                resume_error="3.0.0 is committed on rootfs_b, but ml_package "
                "did not mount",
            ),
        )

        this_boot("boot-3")  # still rootfs_b
        _dpi = FakeDPI(layout=LAYOUT_ON_B)
        _ctrl = make_controller(tmp_path, _dpi)

        assert _dpi.calls == ["layout", "resume"]
        assert not staged_in_boot(tmp_path).exists()
        assert _ctrl.get_booted_ota_status() == OTAStatus.FAILURE
        _info = _ctrl.get_booted_failure_info()
        assert _info is not None
        assert _info[0] == FailureType.UNRECOVERABLE
        assert _info[1].startswith("E557")
        assert "ml_package did not mount" in _info[1]

    def test_a_restart_before_the_reboot_does_not_ask_the_dpi_to_conclude(
        self, tmp_path: Path, this_boot
    ):
        """After the DPI armed the standby slot and before the reboot, otaclient may be
        restarted (a crash, a restart while it waits for a child ECU). The DPI's
        resume would read that as 'staged there but booted here' and consume the
        record while the slot stays armed: it is not asked until a reboot happened."""
        _dir = status_dir(tmp_path, "rootfs_a")
        _dir.mkdir(parents=True)
        (_dir / "status").write_text(OTAStatus.FAILURE.name)
        (_dir / "slot_in_use").write_text("rootfs_b")
        an_update_was_staged_in(tmp_path, "boot-1")  # this very boot
        _dpi = FakeDPI(resume_error="staged 1.7.2 on rootfs_b but booted rootfs_a")

        _ctrl = make_controller(tmp_path, _dpi)

        assert _dpi.calls == ["layout"]
        assert _ctrl.get_booted_ota_status() == OTAStatus.FAILURE
        assert _ctrl.get_booted_failure_info() is None
        assert staged_in_boot(tmp_path).read_text() == "boot-1", "kept for the reboot"

    def test_the_boot_after_a_staging_asks_once_and_forgets(
        self, tmp_path: Path, this_boot
    ):
        """An update that never got to its reboot (the download failed, say) still
        left its mark; the first start after a reboot lets the DPI say 'nothing to
        resume', and later starts in that boot do not ask again."""
        an_update_was_staged_in(tmp_path, "boot-0")
        _first = FakeDPI()
        make_controller(tmp_path, _first)
        assert _first.calls == ["layout", "resume"]
        assert not staged_in_boot(tmp_path).exists()

        _second = FakeDPI()
        make_controller(tmp_path, _second)
        assert _second.calls == ["layout"]

    def test_a_reason_recorded_before_the_reboot_is_kept(
        self, tmp_path: Path, this_boot
    ):
        """An update that failed in the process (a download, say) recorded why; the
        DPI having nothing to commit on the next boot does not replace that."""
        make_controller(tmp_path).store_failure_info(
            failure_type=FailureType.RECOVERABLE,
            failure_reason="E521: resources downloading was interrupted",
        )
        an_update_was_staged_in(tmp_path, "boot-0")

        _ctrl = make_controller(tmp_path, FakeDPI(resume_error="nothing pending"))

        assert _ctrl.get_booted_ota_status() == OTAStatus.FAILURE
        assert _ctrl.get_booted_failure_info() == (
            FailureType.RECOVERABLE,
            "E521: resources downloading was interrupted",
        )

    def test_a_healthy_boot_records_no_failure_when_the_dpi_errs(
        self, tmp_path: Path, this_boot
    ):
        """Only a FAILURE boot carries a reason; a DPI that cannot answer on a boot
        that succeeded does not turn it into one."""
        _dir = status_dir(tmp_path, "rootfs_a")
        _dir.mkdir(parents=True)
        (_dir / "status").write_text(OTAStatus.SUCCESS.name)
        (_dir / "slot_in_use").write_text("rootfs_a")
        an_update_was_staged_in(tmp_path, "boot-0")

        _ctrl = make_controller(
            tmp_path, FakeDPI(resume_error="cannot read the record")
        )

        assert _ctrl.get_booted_ota_status() == OTAStatus.SUCCESS
        assert _ctrl.get_booted_failure_info() is None
        assert (_dir / "status").read_text() == OTAStatus.SUCCESS.name

    def test_an_ordinary_boot_asks_the_dpi_nothing(self, tmp_path: Path, this_boot):
        """No update was staged by otaclient: the DPI's record, if any, is another
        caller's to conclude."""
        _dir = status_dir(tmp_path, "rootfs_a")
        _dir.mkdir(parents=True)
        (_dir / "status").write_text(OTAStatus.SUCCESS.name)
        (_dir / "slot_in_use").write_text("rootfs_a")
        _dpi = FakeDPI()

        _ctrl = make_controller(tmp_path, _dpi)

        assert _dpi.calls == ["layout"]
        assert _ctrl.get_booted_ota_status() == OTAStatus.SUCCESS


class TestUpdateFlow:
    def test_pre_update_records_everything_before_the_dpi_is_asked(
        self, tmp_path: Path, this_boot
    ):
        """Nothing is mounted or erased: the image the DPI writes covers every byte
        of the slot, and the scratch that goes with it is made by the same write.
        What is written is the record: which boot, the current slot's "in flight to
        the standby", and the standby's own UPDATING -- the last one now, because
        the DPI arms the trial boot as part of the write that follows."""
        _dpi = FakeDPI()
        _ctrl = make_controller(tmp_path, _dpi)

        _ctrl.pre_update(standby_as_ref=False, erase_standby=True)

        assert _dpi.calls == ["layout"]  # no mkfs, no mount, no arming
        assert (
            status_dir(tmp_path, "rootfs_a") / "slot_in_use"
        ).read_text() == "rootfs_b"
        _standby = status_dir(tmp_path, "rootfs_b")
        assert (_standby / "status").read_text() == OTAStatus.UPDATING.name
        assert (_standby / "slot_in_use").read_text() == "rootfs_b"
        assert staged_in_boot(tmp_path).read_text() == "boot-1"

    def test_post_update_records_what_was_written(self, tmp_path: Path):
        _ctrl = make_controller(tmp_path)

        _ctrl.post_update("2.9.0")

        _standby = status_dir(tmp_path, "rootfs_b")
        assert (_standby / "status").read_text() == OTAStatus.UPDATING.name
        assert (_standby / "version").read_text() == "2.9.0"
        assert (_standby / "slot_in_use").read_text() == "rootfs_b"

    def test_an_armed_slot_boots_as_a_switching_boot_even_after_an_abort(
        self, tmp_path: Path, this_boot
    ):
        """The DPI arms during the write; an abort or a power cut after that and
        before post_update still leaves the standby recorded as the slot being
        updated, so its trial boot is concluded and reported, not booted as nothing
        in particular."""
        _ctrl = make_controller(tmp_path)
        _ctrl.pre_update(standby_as_ref=False, erase_standby=True)
        _ctrl.on_abort()  # no post_update

        this_boot("boot-2")  # rebooted into the armed standby
        _dpi = FakeDPI(
            layout=SlotLayout(
                active_slot="rootfs_b",
                standby_slot="rootfs_a",
                active_dev="/dev/sda4",
                standby_dev="/dev/sda3",
                platform="grub",
            )
        )
        _after = make_controller(tmp_path, _dpi)

        assert _dpi.calls == ["layout", "resume"]
        assert _after.get_booted_ota_status() == OTAStatus.SUCCESS
        assert _after.load_version() == "2.9.0", "no version recorded: the image's"

    def test_the_images_config_files_are_not_installed_on_this_layout(
        self, tmp_path: Path, caplog
    ):
        """The standby slot is never mounted here; the image build places what the
        image ships for /boot/ota."""
        caplog.set_level(logging.INFO, logger="otaclient.boot_control")
        _ctrl = make_controller(tmp_path)

        _ctrl.post_update("2.9.0", ota_config_files=object())  # type: ignore[arg-type]

        assert "not installed on a partition-based layout" in caplog.text
        assert (status_dir(tmp_path, "rootfs_b") / "version").read_text() == "2.9.0"

    def test_a_failure_leaves_the_armed_slot_alone(self, tmp_path: Path):
        """The armed slot gets one trial boot and is rolled back if it fails, which
        is the design's own safe path; otaclient does not disarm what it did not arm."""
        _dpi = FakeDPI()
        _ctrl = make_controller(tmp_path, _dpi)

        _ctrl.on_operation_failure()

        assert _dpi.calls == ["layout"]
        assert (status_dir(tmp_path, "rootfs_a") / "status").read_text() == (
            OTAStatus.FAILURE.name
        )

    def test_a_record_that_cannot_be_written_is_a_pre_update_failure(
        self, tmp_path: Path, monkeypatch
    ):
        _ctrl = make_controller(tmp_path)

        def _refuse(*args, **kwargs):
            raise OSError(30, "Read-only file system")

        monkeypatch.setattr(_ctrl._ota_status_control, "pre_update_current", _refuse)
        with pytest.raises(ota_errors.BootControlPreUpdateFailed, match="Read-only"):
            _ctrl.pre_update(standby_as_ref=False, erase_standby=True)

    def test_a_record_that_cannot_be_written_is_a_post_update_failure(
        self, tmp_path: Path, monkeypatch
    ):
        _ctrl = make_controller(tmp_path)

        def _refuse(*args, **kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(_ctrl._ota_status_control, "post_update_standby", _refuse)
        with pytest.raises(ota_errors.BootControlPostUpdateFailed, match="No space"):
            _ctrl.post_update("2.9.0")

    def test_finalizing_reboots_through_the_root_it_is_given(
        self, tmp_path: Path, monkeypatch
    ):
        """A dynamically loaded otaclient reboots the host, not its own app image."""
        _seen = []
        monkeypatch.setattr(
            _partition_image.cmdhelper, "reboot", lambda **kw: _seen.append(kw)
        )

        make_controller(tmp_path).finalizing_update(chroot="/host_root")

        assert _seen == [{"chroot": "/host_root"}]


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


def test_a_version_the_dpi_cannot_read_is_left_unknown(tmp_path: Path):
    """No record and a DPI that cannot answer: the version is unknown, which is
    reported as such; otaclient still starts."""
    _ctrl = make_controller(tmp_path, FakeDPI(version_error="no such verb"))

    assert _ctrl.load_active_slot_image_version() == ""
    assert _ctrl.load_version() == ""


class TestWhereTheStateGoes:
    """The status files must outlive the reboot and the staged payload must survive
    until the DPI has consumed it, and where that is differs per device side. Written
    into the slot that is about to be overwritten, the status files are gone exactly
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
        """The grub layout has a shared boot partition mounted rw on every boot. L4T
        has no shared boot partition -- its boot chain is in flash and both rootfs
        slots are overwritten -- so the state goes on optdata, which the platform
        installer mounts at /opt/data. The payload goes on optdata on both sides:
        gigabytes of room, outside the slots, root's own directory -- never the
        scratch (2 GiB, remade on every update) nor a world-writable /var/tmp where
        another user could place the directory first."""
        assert PLATFORMS["grub"].state_dir == "/boot"
        assert PLATFORMS["l4t"].state_dir == "/opt/data"
        assert PLATFORMS["grub"].staging_dir == "/opt/data/otaclient"
        assert PLATFORMS["l4t"].staging_dir == "/opt/data/otaclient"
        for _platform in PLATFORMS.values():
            assert not _platform.staging_dir.startswith(("/var/tmp", "/tmp"))

    def test_a_jetson_keeps_its_state_where_that_platform_keeps_it(
        self, tmp_path: Path
    ):
        _state = tmp_path / "opt-data"

        _ctrl = PartitionImageBootController(
            dpi=FakeDPI(layout=self.jetson_layout()), state_dir=_state
        )

        assert _ctrl.bootloader_type == BootloaderType.JETSON_DPI
        assert status_dir(_state, "APP").is_dir()
        assert _ctrl.payload_staging_dir == Path(PLATFORMS["l4t"].staging_dir)

    def test_a_platform_with_no_known_place_for_it_is_refused(self):
        """Rather than defaulting to /boot, which on that device may be inside the
        slot the update is about to overwrite."""
        with pytest.raises(
            ota_errors.BootControlStartupFailed, match="survive a reboot"
        ):
            PartitionImageBootController(
                dpi=FakeDPI(layout=self.jetson_layout(platform="somewhere-new"))
            )
