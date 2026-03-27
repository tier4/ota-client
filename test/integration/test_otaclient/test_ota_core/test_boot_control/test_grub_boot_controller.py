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
"""Integration tests for the new grub boot controller.

These tests verify:
  - Startup flow: GrubBootController.__init__ correctly sets up the /boot
    filesystem under different initial conditions.
  - Pre-update hooks: GrubBootController._pre_update_prepare_standby and
    _pre_update_platform_specific correctly prepare standby slot for OTA.
  - Post-update hooks: GrubBootController._post_update_platform_specific
    correctly configures boot files, rootfs, and grub for standby slot.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from otaclient._types import OTAStatus
from otaclient.boot_control._grub_common import OTAManagedCfg
from otaclient.boot_control._grub_new import GrubBootController
from otaclient.errors import BootControlPostUpdateFailed, BootControlPreUpdateFailed

from .conftest import (
    GRUB_CFG_CONTENT,
    INITRD_FNAME,
    KERNEL_FNAME,
    KERNEL_VERSION,
    SAMPLE_FSTAB,
    SLOT_B_UUID,
    _create_dummy_kernel,
    _create_rootfs,
)

# ---------------------------------------------------------------------------
# Helpers for setting up different /boot scenarios
# ---------------------------------------------------------------------------


def _setup_new_grub_managed_boot(
    boot_dir: Path,
    *,
    ota_status: str = "SUCCESS",
    slot_in_use: str = "ota-slot_a",
) -> None:
    """Set up /boot as if already managed by the new grub boot control."""
    # --- /boot/ota-slot_a/ with kernel, initrd, and OTA status files ---
    slot_a_dir = boot_dir / "ota-slot_a"
    _create_dummy_kernel(slot_a_dir)
    (slot_a_dir / "grub").mkdir(exist_ok=True)
    (slot_a_dir / "status").write_text(ota_status)
    (slot_a_dir / "slot_in_use").write_text(slot_in_use)
    (slot_a_dir / "version").write_text("2.0.0")

    # --- /boot/ota-slot_b/ (standby, minimal) ---
    slot_b_dir = boot_dir / "ota-slot_b"
    slot_b_dir.mkdir(parents=True, exist_ok=True)
    (slot_b_dir / "grub").mkdir(exist_ok=True)

    # --- /boot/grub/grub.cfg as OTA-managed ---
    managed_cfg = OTAManagedCfg(
        raw_contents=GRUB_CFG_CONTENT.strip(),
        grub_version="2.12",
    )
    (boot_dir / "grub" / "grub.cfg").write_text(managed_cfg.export())

    # --- /boot/grub/grubenv ---
    (boot_dir / "grub" / "grubenv").write_text("saved_entry=ota-slot_a\n")

    # --- /boot/grub/ota-slot_a.cfg ---
    (boot_dir / "grub" / "ota-slot_a.cfg").write_text(
        "menuentry 'ota-slot_a' { linux /ota-slot_a/vmlinuz-" + KERNEL_VERSION + " }\n"
    )

    # --- /boot/efi (created by controller init for UEFI systems) ---
    (boot_dir / "efi").mkdir(exist_ok=True)

    # --- kernel copies at /boot root (backward compat) ---
    (boot_dir / KERNEL_FNAME).write_text("dummy-kernel")
    (boot_dir / INITRD_FNAME).write_text("dummy-initrd")


def _setup_fresh_ecu_boot(boot_dir: Path) -> None:
    """Set up /boot as a fresh ECU, not managed by OTA.

    Kernel and initrd are at /boot root. grub.cfg is a normal
    (non-OTA-managed) file.
    """
    _create_dummy_kernel(boot_dir)
    (boot_dir / "grub" / "grub.cfg").write_text(GRUB_CFG_CONTENT)


def _setup_old_grub_managed_boot(boot_dir: Path) -> None:
    """Set up /boot as if managed by the old grub boot control.

    Old layout:
      /boot/grub/grub.cfg -> ../ota-partition/grub.cfg  (symlink)
      /boot/ota-partition -> ota-partition.sda3           (symlink)
      /boot/ota-partition.sda3/                           (actual files)
      /boot/ota-partition.sda4/                           (standby)
      /boot/vmlinuz-ota -> ota-partition/vmlinuz-ota      (symlink)
      /boot/initrd.img-ota -> ota-partition/initrd.img-ota
      /boot/vmlinuz-ota.standby -> ota-partition.sda4/vmlinuz-ota
      /boot/initrd.img-ota.standby -> ota-partition.sda4/initrd.img-ota

    No actual kernel files at /boot root.
    """
    # --- active slot: ota-partition.sda3 ---
    active_part = boot_dir / "ota-partition.sda3"
    active_part.mkdir(parents=True)
    (active_part / KERNEL_FNAME).write_text("dummy-kernel")
    (active_part / INITRD_FNAME).write_text("dummy-initrd")
    (active_part / "vmlinuz-ota").symlink_to(KERNEL_FNAME)
    (active_part / "initrd.img-ota").symlink_to(INITRD_FNAME)
    (active_part / "grub.cfg").write_text(GRUB_CFG_CONTENT)
    (active_part / "status").write_text("SUCCESS")
    (active_part / "slot_in_use").write_text("sda3")
    (active_part / "version").write_text("1.0.0")

    # --- standby slot: ota-partition.sda4 (minimal) ---
    standby_part = boot_dir / "ota-partition.sda4"
    standby_part.mkdir(parents=True)
    (standby_part / KERNEL_FNAME).write_text("dummy-kernel-standby")
    (standby_part / INITRD_FNAME).write_text("dummy-initrd-standby")
    (standby_part / "vmlinuz-ota").symlink_to(KERNEL_FNAME)
    (standby_part / "initrd.img-ota").symlink_to(INITRD_FNAME)

    # --- /boot/ota-partition symlink -> ota-partition.sda3 ---
    (boot_dir / "ota-partition").symlink_to("ota-partition.sda3")

    # --- /boot/grub/grub.cfg symlink -> ../ota-partition/grub.cfg ---
    grub_cfg_path = boot_dir / "grub" / "grub.cfg"
    grub_cfg_path.unlink(missing_ok=True)
    grub_cfg_path.symlink_to("../ota-partition/grub.cfg")

    # --- global ota kernel/initrd symlinks (no actual files at /boot root) ---
    (boot_dir / "vmlinuz-ota").symlink_to("ota-partition/vmlinuz-ota")
    (boot_dir / "initrd.img-ota").symlink_to("ota-partition/initrd.img-ota")
    (boot_dir / "vmlinuz-ota.standby").symlink_to("ota-partition.sda4/vmlinuz-ota")
    (boot_dir / "initrd.img-ota.standby").symlink_to(
        "ota-partition.sda4/initrd.img-ota"
    )


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------


def _assert_ota_managed_grub_cfg(boot_dir: Path) -> None:
    """Assert /boot/grub/grub.cfg is a valid OTA-managed config file."""
    grub_cfg_path = boot_dir / "grub" / "grub.cfg"
    assert grub_cfg_path.is_file()
    assert (
        not grub_cfg_path.is_symlink()
    ), "grub.cfg must be a regular file, not a symlink"

    content = grub_cfg_path.read_text()
    validated = OTAManagedCfg.validate_managed_config(content)
    assert validated is not None, "grub.cfg must be a valid OTA-managed config"
    assert validated.grub_version == "2.12"


def _assert_slot_boot_dir(boot_dir: Path, slot_id: str) -> None:
    """Assert that the slot boot directory has the expected structure."""
    slot_dir = boot_dir / slot_id
    assert slot_dir.is_dir()
    assert (slot_dir / KERNEL_FNAME).is_file()
    assert (slot_dir / INITRD_FNAME).is_file()
    assert (slot_dir / "grub").is_dir(), "slot dir must have a dummy grub/ subdir"


def _assert_grubenv_contains(boot_dir: Path, expected_entry: str) -> None:
    """Assert grubenv contains the expected saved_entry."""
    grubenv = boot_dir / "grub" / "grubenv"
    assert grubenv.is_file()
    content = grubenv.read_text()
    assert expected_entry in content


def _assert_slot_boot_cfg(boot_dir: Path, slot_id: str) -> None:
    """Assert the per-slot boot config file exists."""
    cfg_path = boot_dir / "grub" / f"{slot_id}.cfg"
    assert cfg_path.is_file()
    content = cfg_path.read_text()
    assert f"'{slot_id}'" in content, f"boot cfg must reference slot id {slot_id}"
    assert KERNEL_VERSION in content, "boot cfg must reference the kernel version"


def _assert_old_grub_files_cleaned_up(boot_dir: Path) -> None:
    """Assert all old grub boot control artifacts are removed."""
    assert not (
        boot_dir / "ota-partition"
    ).exists(), "/boot/ota-partition should be removed"

    for pattern in ["ota-partition.sda*", "vmlinuz-ota*", "initrd.img-ota*"]:
        matches = list(boot_dir.glob(pattern))
        assert not matches, f"old files matching {pattern} should be removed: {matches}"


def _assert_boot_root_kernel_copies(boot_dir: Path) -> None:
    """Assert kernel/initrd copies exist at /boot root (backward compat)."""
    assert (boot_dir / KERNEL_FNAME).is_file()
    assert (boot_dir / INITRD_FNAME).is_file()


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class TestGrubBootControllerNormalStartup:
    """Case 1: Controller starts on an already OTA-managed ECU."""

    @pytest.fixture(autouse=True)
    def setup_env(
        self, boot_dir, rootfs_dir, mnt_dir, mock_grub_commands, redirect_paths
    ):
        """Ensure all mocks and path redirections are active."""

    def test_startup_ota_status_success(self, boot_dir: Path):
        """Case 1a: OTA status is SUCCESS — no bootstrap, boot unchanged."""
        _setup_new_grub_managed_boot(boot_dir, ota_status="SUCCESS")
        # snapshot boot dir before init
        files_before = set(boot_dir.rglob("*"))

        controller = GrubBootController()

        assert controller._boot_control.resetup_requested is False
        assert controller.get_booted_ota_status() == OTAStatus.SUCCESS

        # /boot should be unchanged
        files_after = set(boot_dir.rglob("*"))
        assert files_before == files_after

    def test_startup_ota_status_updating(self, boot_dir: Path):
        """Case 1b: OTA status is UPDATING — finalize_switch_boot is triggered."""
        _setup_new_grub_managed_boot(
            boot_dir, ota_status="UPDATING", slot_in_use="ota-slot_a"
        )

        controller = GrubBootController()

        assert controller._boot_control.resetup_requested is False
        # finalize_update_switch_boot was called, status should now be SUCCESS
        assert controller.get_booted_ota_status() == OTAStatus.SUCCESS
        # grubenv should be updated with saved_entry for current slot
        _assert_grubenv_contains(boot_dir, "saved_entry=ota-slot_a")

    def test_startup_migrate_from_old_grub_success(
        self, boot_dir: Path, rootfs_dir: Path
    ):
        """Case 1c: Previous OTA was handled by old grub boot controller.

        When the old grub boot control layout is detected, the new grub boot
        controller triggers a bootstrap to migrate to the new layout. The final
        OTA status is INITIALIZED. INITIALIZED status is considered to be
        SUCCESS on FMS.
        """
        _setup_old_grub_managed_boot(boot_dir)

        controller = GrubBootController()

        # bootstrap was triggered due to old layout detection
        assert controller._boot_control.resetup_requested is True
        assert controller.get_booted_ota_status() == OTAStatus.INITIALIZED

        # new layout created
        _assert_slot_boot_dir(boot_dir, "ota-slot_a")
        _assert_slot_boot_cfg(boot_dir, "ota-slot_a")
        _assert_ota_managed_grub_cfg(boot_dir)
        _assert_grubenv_contains(boot_dir, "saved_entry=ota-slot_a")
        _assert_boot_root_kernel_copies(boot_dir)

        # old files cleaned up
        _assert_old_grub_files_cleaned_up(boot_dir)


class TestGrubBootControllerFreshECU:
    """Case 2: Controller starts on a fresh ECU not managed by OTA."""

    @pytest.fixture(autouse=True)
    def setup_env(
        self, boot_dir, rootfs_dir, mnt_dir, mock_grub_commands, redirect_paths
    ):
        """Ensure all mocks and path redirections are active."""

    def test_bootstrap_from_fresh_ecu(self, boot_dir: Path, rootfs_dir: Path):
        """Fresh ECU triggers full bootstrap: slot dirs, boot cfg, grub.cfg, grubenv."""
        _setup_fresh_ecu_boot(boot_dir)

        controller = GrubBootController()

        # bootstrap was triggered
        assert controller._boot_control.resetup_requested is True
        assert controller.get_booted_ota_status() == OTAStatus.INITIALIZED

        # /boot/ota-slot_a/ created with kernel and initrd
        _assert_slot_boot_dir(boot_dir, "ota-slot_a")

        # /boot/grub/ota-slot_a.cfg created
        _assert_slot_boot_cfg(boot_dir, "ota-slot_a")

        # /boot/grub/grub.cfg is OTA-managed
        _assert_ota_managed_grub_cfg(boot_dir)

        # grubenv updated
        _assert_grubenv_contains(boot_dir, "saved_entry=ota-slot_a")

        # kernel copies at /boot root (backward compat)
        _assert_boot_root_kernel_copies(boot_dir)

        # NOTE: 30_ota hook is written into a bind-mounted TemporaryDirectory
        # that is cleaned up after bootstrap, so we cannot assert on it here.


class TestGrubBootControllerMigrateFromOldGrub:
    """Case 3: Controller starts on an ECU managed by the old grub boot control."""

    @pytest.fixture(autouse=True)
    def setup_env(
        self, boot_dir, rootfs_dir, mnt_dir, mock_grub_commands, redirect_paths
    ):
        """Ensure all mocks and path redirections are active."""

    def test_migrate_from_old_grub(self, boot_dir: Path, rootfs_dir: Path):
        """Old grub layout triggers bootstrap, migrates to new layout, cleans up old files."""
        _setup_old_grub_managed_boot(boot_dir)

        # verify old layout is in place before migration
        assert (boot_dir / "grub" / "grub.cfg").is_symlink()
        assert (boot_dir / "ota-partition").is_symlink()
        assert (boot_dir / "ota-partition.sda3").is_dir()

        controller = GrubBootController()

        # bootstrap was triggered
        assert controller._boot_control.resetup_requested is True
        assert controller.get_booted_ota_status() == OTAStatus.INITIALIZED

        # new layout created
        _assert_slot_boot_dir(boot_dir, "ota-slot_a")
        _assert_slot_boot_cfg(boot_dir, "ota-slot_a")

        # grub.cfg is now a regular file (NOT symlink), OTA-managed
        _assert_ota_managed_grub_cfg(boot_dir)

        # grubenv updated
        _assert_grubenv_contains(boot_dir, "saved_entry=ota-slot_a")

        # kernel copies at /boot root
        _assert_boot_root_kernel_copies(boot_dir)

        # old files cleaned up
        _assert_old_grub_files_cleaned_up(boot_dir)

        # NOTE: 30_ota hook is written into a bind-mounted TemporaryDirectory
        # that is cleaned up after bootstrap, so we cannot assert on it here.


# ---------------------------------------------------------------------------
# Pre-update hook tests
# ---------------------------------------------------------------------------


class TestGrubBootControllerPreUpdate:
    """Test pre_update template method and its GRUB-specific hooks.

    Verifies:
    - _pre_update_prepare_standby passes fsuuid to prepare_standby_dev
    - _pre_update_platform_specific cleans standby boot dir and cfg
    - OTA status files are updated (current slot -> FAILURE)
    - Execution order of template method steps
    - Exception wrapping in BootControlPreUpdateFailed
    """

    @pytest.fixture(autouse=True)
    def setup_env(
        self, boot_dir, rootfs_dir, mnt_dir, mock_grub_commands, redirect_paths
    ):
        """Ensure all mocks and path redirections are active."""

    def _create_ready_controller(self, boot_dir: Path, mocker: MockerFixture):
        """Set up a SUCCESS-state controller with stale standby artifacts.

        Returns (controller, mock_prepare, mock_mount_standby, mock_mount_active).
        """
        _setup_new_grub_managed_boot(boot_dir)

        # Add stale standby boot artifacts that pre_update should clean up
        slot_b_dir = boot_dir / "ota-slot_b"
        slot_b_dir.mkdir(parents=True, exist_ok=True)
        (slot_b_dir / "grub").mkdir(exist_ok=True)
        (slot_b_dir / "vmlinuz-old").write_text("old-kernel")
        (slot_b_dir / "initrd-old").write_text("old-initrd")
        (slot_b_dir / "status").write_text("SUCCESS")

        # Add stale standby boot cfg
        (boot_dir / "grub" / "ota-slot_b.cfg").write_text("old boot cfg content")

        controller = GrubBootController()

        # Mock SlotMountHelper methods that call system commands
        # (these go through _slot_mnt_helper's own cmdhelper import)
        mock_prepare = mocker.MagicMock()
        mock_mount_standby = mocker.MagicMock()
        mock_mount_active = mocker.MagicMock()
        controller._mp_control.prepare_standby_dev = mock_prepare
        controller._mp_control.mount_standby = mock_mount_standby
        controller._mp_control.mount_active = mock_mount_active

        return controller, mock_prepare, mock_mount_standby, mock_mount_active

    @pytest.mark.parametrize("erase_standby", [True, False])
    def test_pre_update_erase_standby(
        self, boot_dir: Path, erase_standby: bool, mocker: MockerFixture
    ):
        """Verify pre_update correctly prepares standby and cleans up boot artifacts."""
        controller, mock_prepare, mock_mount_standby, mock_mount_active = (
            self._create_ready_controller(boot_dir, mocker)
        )

        controller.pre_update(
            standby_as_ref=not erase_standby, erase_standby=erase_standby
        )

        # Hook 1: prepare_standby_dev called with correct args
        mock_prepare.assert_called_once_with(
            erase_standby=erase_standby, fsuuid=SLOT_B_UUID
        )

        # Mounts called
        mock_mount_standby.assert_called_once()
        mock_mount_active.assert_called_once()

        # OTA status files updated on current slot (slot_a)
        assert (boot_dir / "ota-slot_a" / "status").read_text() == "FAILURE"
        assert (boot_dir / "ota-slot_a" / "slot_in_use").read_text() == "ota-slot_b"

        # Hook 2: standby boot dir wiped and only grub/ subdir remains
        slot_b_dir = boot_dir / "ota-slot_b"
        assert slot_b_dir.is_dir()
        children = list(slot_b_dir.iterdir())
        assert len(children) == 1
        assert children[0].name == "grub"
        assert children[0].is_dir()
        # Stale files must be gone
        assert not (slot_b_dir / "vmlinuz-old").exists()
        assert not (slot_b_dir / "initrd-old").exists()
        assert not (slot_b_dir / "status").exists()

        # Hook 2: standby boot cfg removed
        assert not (boot_dir / "grub" / "ota-slot_b.cfg").exists()

    def test_pre_update_call_order(self, boot_dir: Path, mocker: MockerFixture):
        """Verify the template method executes steps in the correct order."""
        controller, mock_prepare, mock_mount_standby, mock_mount_active = (
            self._create_ready_controller(boot_dir, mocker)
        )

        call_order = []

        # Wrap pre_update_current to track ordering
        original_pre_update_current = controller._ota_status_control.pre_update_current

        def tracked_pre_update_current():
            call_order.append("pre_update_current")
            original_pre_update_current()

        controller._ota_status_control.pre_update_current = tracked_pre_update_current

        # Track mock calls via side_effect
        mock_prepare.side_effect = lambda **kw: call_order.append("prepare_standby_dev")
        mock_mount_standby.side_effect = lambda: call_order.append("mount_standby")
        mock_mount_active.side_effect = lambda: call_order.append("mount_active")

        # Wrap platform_specific to track ordering
        original_platform_specific = controller._pre_update_platform_specific

        def tracked_platform_specific(**kw):
            call_order.append("platform_specific")
            original_platform_specific(**kw)

        controller._pre_update_platform_specific = tracked_platform_specific

        controller.pre_update(standby_as_ref=False, erase_standby=True)

        assert call_order == [
            "pre_update_current",
            "prepare_standby_dev",
            "mount_standby",
            "mount_active",
            "platform_specific",
        ]

    def test_pre_update_wraps_exception(self, boot_dir: Path, mocker: MockerFixture):
        """Verify exceptions from hooks are wrapped in BootControlPreUpdateFailed."""
        controller, mock_prepare, _, _ = self._create_ready_controller(boot_dir, mocker)
        mock_prepare.side_effect = RuntimeError("disk error")

        with pytest.raises(BootControlPreUpdateFailed):
            controller.pre_update(standby_as_ref=False, erase_standby=True)


# ---------------------------------------------------------------------------
# Post-update tests
# ---------------------------------------------------------------------------


def _setup_standby_rootfs_for_post_update(mnt_dir: Path, rootfs_dir: Path) -> None:
    """Simulate standby rootfs after OTA image has been written.

    In real operation, pre_update mounts the standby slot and the OTA
    updater writes the new image. This helper creates the minimal
    filesystem state expected by _post_update_platform_specific.
    """
    standby_mp = mnt_dir / "standby"

    # Kernel at <standby_mp>/boot/
    standby_boot = standby_mp / "boot"
    standby_boot.mkdir(parents=True, exist_ok=True)
    (standby_boot / KERNEL_FNAME).write_text("new-kernel")
    (standby_boot / INITRD_FNAME).write_text("new-initrd")

    # Config files at <standby_mp>/etc/
    _create_rootfs(standby_mp)

    # OTA config files from the new image
    standby_ota = standby_mp / "boot" / "ota"
    standby_ota.mkdir(parents=True, exist_ok=True)
    (standby_ota / "proxy_info.yaml").write_text("new-proxy-config")
    (standby_ota / "ecu_info.yaml").write_text("new-ecu-config")

    # Active slot mount (bind-mounted ro in real life)
    active_mp = mnt_dir / "active"
    (active_mp / "etc").mkdir(parents=True, exist_ok=True)
    (active_mp / "etc" / "fstab").write_text(SAMPLE_FSTAB)


NEW_KERNEL_VERSION = "6.12.0-1-generic"
NEW_KERNEL_FNAME = f"vmlinuz-{NEW_KERNEL_VERSION}"
NEW_INITRD_FNAME = f"initrd.img-{NEW_KERNEL_VERSION}"


class TestGrubBootControllerPostUpdate:
    """E2E and targeted tests for the full post_update workflow.

    Covers:
    - Full post_update e2e: boot files, rootfs config, boot cfg, grubenv,
      OTA status, OTA config files, cleanup
    - ecu_info.yaml conditional install
    - Old kernel/initrd cleanup at /boot root
    - Exception wrapping
    """

    @pytest.fixture(autouse=True)
    def setup_env(
        self, boot_dir, rootfs_dir, mnt_dir, mock_grub_commands, redirect_paths
    ):
        """Ensure all mocks and path redirections are active."""

    def _create_post_update_ready_controller(
        self,
        boot_dir: Path,
        mnt_dir: Path,
        rootfs_dir: Path,
        mocker: MockerFixture,
    ):
        """Set up the full initial environment and return the controller.

        Initial state simulates: __init__ (SUCCESS) + pre_update done +
        OTA image written to standby rootfs. Only SlotMountHelper methods
        that call system commands are mocked.

        Returns (controller, mock_umount_all).
        """
        # --- /boot as a SUCCESS-state OTA-managed system ---
        _setup_new_grub_managed_boot(boot_dir)

        # pre_update_platform_specific leaves standby with only grub/ subdir
        slot_b_dir = boot_dir / "ota-slot_b"
        slot_b_dir.mkdir(parents=True, exist_ok=True)
        (slot_b_dir / "grub").mkdir(exist_ok=True)

        # pre_update_current sets current slot status
        (boot_dir / "ota-slot_a" / "status").write_text("FAILURE")
        (boot_dir / "ota-slot_a" / "slot_in_use").write_text("ota-slot_b")

        # pre_update removed standby boot cfg
        (boot_dir / "grub" / "ota-slot_b.cfg").unlink(missing_ok=True)

        # --- Standby rootfs with new OTA image ---
        _setup_standby_rootfs_for_post_update(mnt_dir, rootfs_dir)

        # --- Construct controller ---
        controller = GrubBootController()

        # Mock only SlotMountHelper methods (real syscalls)
        controller._mp_control.prepare_standby_dev = mocker.MagicMock()
        controller._mp_control.mount_standby = mocker.MagicMock()
        controller._mp_control.mount_active = mocker.MagicMock()
        mock_umount_all = mocker.MagicMock()
        controller._mp_control.umount_all = mock_umount_all

        return controller, mock_umount_all

    def test_post_update_e2e(
        self,
        boot_dir: Path,
        mnt_dir: Path,
        rootfs_dir: Path,
        mocker: MockerFixture,
    ):
        """Run the full post_update workflow and verify all final state."""
        controller, mock_umount_all = self._create_post_update_ready_controller(
            boot_dir, mnt_dir, rootfs_dir, mocker
        )

        controller.post_update(update_version="3.0.0")

        # === OTA status files on standby slot (boot_dir/ota-slot_b/) ===
        slot_b_dir = boot_dir / "ota-slot_b"
        assert slot_b_dir.is_dir()
        assert (slot_b_dir / "status").read_text() == "UPDATING"
        assert (slot_b_dir / "version").read_text() == "3.0.0"
        assert (slot_b_dir / "slot_in_use").read_text() == "ota-slot_b"

        # === Boot files copied to standby boot slot dir ===
        assert (slot_b_dir / KERNEL_FNAME).is_file()
        assert (slot_b_dir / KERNEL_FNAME).read_text() == "new-kernel"
        assert (slot_b_dir / INITRD_FNAME).is_file()
        assert (slot_b_dir / INITRD_FNAME).read_text() == "new-initrd"

        # === Backward compat copies at /boot root ===
        assert (boot_dir / KERNEL_FNAME).is_file()
        assert (boot_dir / INITRD_FNAME).is_file()

        # === Standby rootfs: fstab updated with SLOT_B_UUID for root ===
        standby_fstab = (mnt_dir / "standby" / "etc" / "fstab").read_text()
        assert f"UUID={SLOT_B_UUID}" in standby_fstab

        # === Standby rootfs: /etc/default/grub updated with OTA defaults ===
        standby_grub_default = (
            mnt_dir / "standby" / "etc" / "default" / "grub"
        ).read_text()
        assert "GRUB_TIMEOUT=0" in standby_grub_default
        assert "GRUB_DEFAULT=saved" in standby_grub_default
        assert "GRUB_DISABLE_SUBMENU=y" in standby_grub_default

        # === Standby rootfs: 30_ota hook injected and executable ===
        ota_hook = mnt_dir / "standby" / "etc" / "grub.d" / "30_ota"
        assert ota_hook.is_file()
        assert ota_hook.stat().st_mode & 0o111 != 0, "30_ota must be executable"

        # === OTA config files installed ===
        ota_dir = rootfs_dir / "boot" / "ota"
        assert (ota_dir / "proxy_info.yaml").read_text() == "new-proxy-config"
        assert (ota_dir / "ecu_info.yaml").read_text() == "new-ecu-config"

        # === Per-slot boot cfg generated ===
        _assert_slot_boot_cfg(boot_dir, "ota-slot_b")

        # === Grubenv: next_entry for one-time reboot to standby ===
        _assert_grubenv_contains(boot_dir, "saved_entry=ota-slot_a")
        _assert_grubenv_contains(boot_dir, "next_entry=ota-slot_b")

        # === Active slot (slot_a) unchanged ===
        assert (boot_dir / "ota-slot_a" / "status").read_text() == "FAILURE"
        assert (boot_dir / "ota-slot_a" / "version").read_text() == "2.0.0"

        # === grub.cfg unchanged (not touched by post_update) ===
        _assert_ota_managed_grub_cfg(boot_dir)

        # === Cleanup ===
        mock_umount_all.assert_called_once_with(ignore_error=True)

    def test_post_update_ecu_info_not_overwritten(
        self,
        boot_dir: Path,
        mnt_dir: Path,
        rootfs_dir: Path,
        mocker: MockerFixture,
    ):
        """ecu_info.yaml at destination is preserved if it already exists."""
        controller, _ = self._create_post_update_ready_controller(
            boot_dir, mnt_dir, rootfs_dir, mocker
        )

        # Pre-create ecu_info.yaml at destination
        ota_dir = rootfs_dir / "boot" / "ota"
        ota_dir.mkdir(parents=True, exist_ok=True)
        (ota_dir / "ecu_info.yaml").write_text("original-ecu-config")

        controller.post_update(update_version="3.0.0")

        # proxy_info.yaml always overwritten
        assert (ota_dir / "proxy_info.yaml").read_text() == "new-proxy-config"
        # ecu_info.yaml NOT overwritten — original content preserved
        assert (ota_dir / "ecu_info.yaml").read_text() == "original-ecu-config"

    def test_post_update_wraps_exception(
        self,
        boot_dir: Path,
        mnt_dir: Path,
        rootfs_dir: Path,
        mocker: MockerFixture,
    ):
        """Exception from hook is wrapped in BootControlPostUpdateFailed."""
        _setup_new_grub_managed_boot(boot_dir)
        controller = GrubBootController()
        controller._mp_control.umount_all = mocker.MagicMock()

        # No kernel in standby rootfs → detect_slot_kernel_ver raises
        with pytest.raises(BootControlPostUpdateFailed):
            controller.post_update(update_version="3.0.0")

    # --- Boot file cleanup tests ---

    def _create_boot_cleanup_controller(
        self, boot_dir: Path, mnt_dir: Path, rootfs_dir: Path, mocker: MockerFixture
    ):
        """Set up a controller with old kernel files at /boot root.

        Mocks _install_new_ota_config_files and setup_ota_boot_cfg_for_slot
        to isolate boot file cleanup testing with a different kernel version
        on the standby slot.
        """
        _setup_new_grub_managed_boot(boot_dir)

        # Add old kernel/initrd files at /boot root that should be cleaned up
        old_versions = ["5.15.0-100-generic", "5.19.0-50-generic"]
        for ver in old_versions:
            (boot_dir / f"vmlinuz-{ver}").write_text(f"old-kernel-{ver}")
            (boot_dir / f"initrd.img-{ver}").write_text(f"old-initrd-{ver}")

        controller = GrubBootController()

        # Mock SlotMountHelper methods
        controller._mp_control.prepare_standby_dev = mocker.MagicMock()
        controller._mp_control.mount_standby = mocker.MagicMock()
        controller._mp_control.mount_active = mocker.MagicMock()
        controller._mp_control.umount_all = mocker.MagicMock()

        # Mock methods that access paths not redirected in tests or require
        # matching kernel version in grub.cfg template
        controller._install_new_ota_config_files = mocker.MagicMock()
        controller._boot_control.setup_ota_boot_cfg_for_slot = mocker.MagicMock()

        # Set up standby slot mount point with a new kernel version
        standby_mp = mnt_dir / "standby"
        standby_boot = standby_mp / "boot"
        standby_boot.mkdir(parents=True, exist_ok=True)
        (standby_boot / NEW_KERNEL_FNAME).write_text("new-standby-kernel")
        (standby_boot / NEW_INITRD_FNAME).write_text("new-standby-initrd")

        # Set up rootfs in standby mount for setup_slot_rootfs_for_ota_boot
        _create_rootfs(standby_mp)

        # Set up active slot mount point with fstab for reference
        active_mp = mnt_dir / "active"
        (active_mp / "etc").mkdir(parents=True, exist_ok=True)
        (active_mp / "etc" / "fstab").write_text(SAMPLE_FSTAB)

        return controller

    def test_post_update_cleans_old_boot_files(
        self, boot_dir: Path, mnt_dir: Path, rootfs_dir: Path, mocker: MockerFixture
    ):
        """Verify post_update removes old kernel/initrd from /boot root,
        keeping only active and standby slot versions."""
        controller = self._create_boot_cleanup_controller(
            boot_dir, mnt_dir, rootfs_dir, mocker
        )

        # Run pre_update first (required before post_update)
        controller.pre_update(standby_as_ref=False, erase_standby=True)
        controller.post_update("3.0.0")

        # Active slot's kernel files should be kept
        assert (boot_dir / KERNEL_FNAME).is_file()
        assert (boot_dir / INITRD_FNAME).is_file()

        # New standby kernel files should be present (copied by setup_boot_slot_dir)
        assert (boot_dir / NEW_KERNEL_FNAME).is_file()
        assert (boot_dir / NEW_INITRD_FNAME).is_file()

        # Old kernel/initrd files should be removed
        assert not (boot_dir / "vmlinuz-5.15.0-100-generic").exists()
        assert not (boot_dir / "initrd.img-5.15.0-100-generic").exists()
        assert not (boot_dir / "vmlinuz-5.19.0-50-generic").exists()
        assert not (boot_dir / "initrd.img-5.19.0-50-generic").exists()

    def test_post_update_keeps_non_kernel_files(
        self, boot_dir: Path, mnt_dir: Path, rootfs_dir: Path, mocker: MockerFixture
    ):
        """Verify post_update does not remove non-kernel files from /boot root."""
        controller = self._create_boot_cleanup_controller(
            boot_dir, mnt_dir, rootfs_dir, mocker
        )

        # Add a non-kernel file at /boot root
        (boot_dir / "config-6.11.0-29-generic").write_text("kernel config")
        (boot_dir / "System.map-6.11.0-29-generic").write_text("system map")

        controller.pre_update(standby_as_ref=False, erase_standby=True)
        controller.post_update("3.0.0")

        # Non-kernel files should be untouched
        assert (boot_dir / "config-6.11.0-29-generic").is_file()
        assert (boot_dir / "System.map-6.11.0-29-generic").is_file()

    def test_post_update_same_kernel_version(
        self, boot_dir: Path, mnt_dir: Path, rootfs_dir: Path, mocker: MockerFixture
    ):
        """Verify post_update works when active and standby have the same kernel version."""
        _setup_new_grub_managed_boot(boot_dir)

        # Add old kernel files
        (boot_dir / "vmlinuz-5.15.0-100-generic").write_text("old-kernel")
        (boot_dir / "initrd.img-5.15.0-100-generic").write_text("old-initrd")

        controller = GrubBootController()

        # Mock SlotMountHelper methods
        controller._mp_control.prepare_standby_dev = mocker.MagicMock()
        controller._mp_control.mount_standby = mocker.MagicMock()
        controller._mp_control.mount_active = mocker.MagicMock()
        controller._mp_control.umount_all = mocker.MagicMock()
        controller._install_new_ota_config_files = mocker.MagicMock()

        # Standby has same kernel version as active
        standby_mp = mnt_dir / "standby"
        standby_boot = standby_mp / "boot"
        standby_boot.mkdir(parents=True, exist_ok=True)
        (standby_boot / KERNEL_FNAME).write_text("same-kernel")
        (standby_boot / INITRD_FNAME).write_text("same-initrd")
        _create_rootfs(standby_mp)

        active_mp = mnt_dir / "active"
        (active_mp / "etc").mkdir(parents=True, exist_ok=True)
        (active_mp / "etc" / "fstab").write_text(SAMPLE_FSTAB)

        controller.pre_update(standby_as_ref=False, erase_standby=True)
        controller.post_update("3.0.0")

        # Active/standby kernel files kept
        assert (boot_dir / KERNEL_FNAME).is_file()
        assert (boot_dir / INITRD_FNAME).is_file()

        # Old files removed
        assert not (boot_dir / "vmlinuz-5.15.0-100-generic").exists()
        assert not (boot_dir / "initrd.img-5.15.0-100-generic").exists()
