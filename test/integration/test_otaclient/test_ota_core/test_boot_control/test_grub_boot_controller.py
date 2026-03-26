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
"""Integration tests for the new grub boot controller startup flow.

These tests verify that GrubBootController.__init__ correctly sets up the
/boot filesystem under different initial conditions:
  1a. Normal startup with OTA status SUCCESS
  1b. Normal startup with OTA status UPDATING (first reboot after update)
  2.  Fresh ECU — grub not managed by OTA
  3.  Migration from old grub boot control to new
"""

from __future__ import annotations

from pathlib import Path

import pytest

from otaclient._types import OTAStatus
from otaclient.boot_control._grub_common import OTAManagedCfg
from otaclient.boot_control._grub_new import GrubBootController

from .conftest import (
    GRUB_CFG_CONTENT,
    INITRD_FNAME,
    KERNEL_FNAME,
    KERNEL_VERSION,
    _create_dummy_kernel,
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

        assert controller._boot_control.initialized is False
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

        assert controller._boot_control.initialized is False
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
        assert controller._boot_control.initialized is True
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
        assert controller._boot_control.initialized is True
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
        assert controller._boot_control.initialized is True
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
