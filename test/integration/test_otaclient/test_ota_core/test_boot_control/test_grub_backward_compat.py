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
"""Integration / end-to-end backward-compatibility tests for the new grub
boot controller.

The top of this file (``TestMigrateOldGrubStatus`` …
``TestGroupABootstrapInputs``) constructs the full ``GrubBootController`` and
exercises the controller's bootstrap, migration, mirror, wipe, and
post-update flows through their real call paths (``OTAStatusFilesControl``,
the slot-mount helper, the orchestration layer in
``BootControllerBase.post_update``).

The bottom of this file (``TestBootstrapSetupRootfsForOtaBoot`` …
``TestTranslateSlotInUseOldToNew``) holds focused tests against individual
``_GrubBootControl`` helpers that touch the real filesystem (write
fstab/grub.cfg, create hardlinks, follow symlinks). They use a partially
mocked controller (bypassing ``__init__`` and stubbing ``boot_slots``) to
keep each test scoped to one helper, but still exercise real I/O so they
belong in the integration tier.

Based on the git history for the old grub boot control, we divide the two
supported old-otaclient groups by their bootstrap-predicate shape:

1. GROUP A: otaclient >= 3.8, < 3.10
2. GROUP B: otaclient >= 3.10, < 3.14

GROUP A and B are divided since https://github.com/tier4/ota-client/pull/601.

The four backward-compat goals (mapped to the test classes that cover them):

1. **No bootstrap on new-managed system (Group B).** FRESH bootstrap
   populates the legacy ``ota-partition.sda<active_pid>/`` folder with
   real-file ``vmlinuz-<uname-r>`` and ``initrd.img-<uname-r>`` so a Group B
   old controller's bootstrap predicate (the AND of those two
   ``is_file(...)`` checks) passes. The standby legacy folder is NOT
   pre-created.
   → ``TestFreshBootstrapLegacyMirror``.
2. **Old Group B controller can OTA the new-managed system.** The mirror
   places the kernel as a real (non-symlink) hardlink so Group B's
   ``_prepare_kernel_initrd_links`` (``not f.is_symlink()`` filter) can pick
   it up.
   → ``TestFreshBootstrapLegacyMirror``.
3. **Preserve OTA status across migration.**
   ``_migrate_status_files_from_old_grub_control`` carries ``status``,
   ``version``, ``version_detail``, and ``slot_in_use`` from the old legacy
   folder into the new slot folder. ``status`` / ``version`` /
   ``version_detail`` are hardlinked (same filesystem, detection-compatible,
   atomic-write-safe); ``slot_in_use`` is translated from ``"sda<pid>"`` to
   ``"ota-slot_<a/b>"`` and written via ``write_str_to_file_atomic``.
   → ``TestMigrateOldGrubStatus`` (single-step migration) and
     ``TestMigrateThenOTAFullCycle`` (migrate → subsequent new-grub OTA).
4. **Group A controller can bootstrap on the new-managed system.** The new
   controller keeps ``/boot/<vmlinuz-X>`` and ``/boot/<initrd.img-X>``
   populated as real files, which is what Group A's
   ``_get_current_booted_files()`` and bootstrap copy step rely on. Group
   A's predicate signals (``vmlinuz-ota`` symlinks, ``/boot/ota-partition``
   symlink, OTA-named ``BOOT_IMAGE=``) are intentionally NOT satisfied —
   Group A always bootstraps and takes the system over.
   → ``TestGroupABootstrapInputs`` and
     ``TestFreshBootstrapLegacyMirror::test_fresh_does_not_satisfy_group_a_bootstrap_predicate``.

Cross-version scenarios these tests cover (preconditions only; the old
controller's own code is not invoked here):

* **new-grub-managed → Group B old otaclient runs → Group B OTA.** Goal-1
  predicate inputs (``TestFreshBootstrapLegacyMirror::
  test_fresh_satisfies_group_b_bootstrap_predicate``); goal-2 OTA
  precondition (real-file kernel via hardlink); post-OTA wipe so a Group B
  old image installed via OTA bootstraps fresh
  (``TestPostUpdateLegacyWipe::test_post_update_e2e_wipes_standby_legacy``).
* **new-grub-managed → Group A old otaclient runs.**
  ``TestFreshBootstrapLegacyMirror::
  test_fresh_does_not_satisfy_group_a_bootstrap_predicate`` confirms the
  three Group-A predicate signals are absent on a new-managed system, so
  bootstrap fires; ``TestGroupABootstrapInputs`` confirms the bootstrap
  PATH succeeds (``/boot/<vmlinuz-X>`` and ``/boot/<initrd.img-X>`` real
  files present).
* **old-grub-managed → new otaclient runs.** ``TestMigrateOldGrubStatus``
  covers status carryover; ``TestMigrateThenOTAFullCycle`` extends that to
  a subsequent new-grub OTA cycle on the migrated system.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from otaclient._types import OTAStatus
from otaclient.boot_control._grub_common import (
    BootFiles,
    OTAManagedCfg,
    OTASlotBootID,
)
from otaclient.boot_control._grub_new import (
    INITRD_PREFIX,
    VMLINUZ_PREFIX,
    GrubBootController,
    _detect_boot_control_setup_case,
    _GrubBootControl,
    _GrubBootControlSetupCase,
)

from .conftest import (
    GRUB_CFG_CONTENT,
    GRUB_INITRD_FNAME,
    GRUB_KERNEL_FNAME,
    GRUB_KERNEL_VERSION,
    GRUB_NEW_INITRD_FNAME,
    GRUB_NEW_KERNEL_FNAME,
    GRUB_NEW_KERNEL_VERSION,
    GRUB_SAMPLE_FSTAB,
    _create_grub_rootfs,
    _setup_fresh_grub_ecu_boot,
    _setup_new_grub_managed_boot,
    _setup_old_grub_managed_boot,
)

#
# ------------ Helpers ------------ #
#
# Used by the focused per-helper tests at the bottom of the file.


def _fstab_entry(
    uuid: str, mp: str, fstype: str, opts: str, dump: str, pass_: str
) -> str:
    return f"UUID={uuid}\t{mp}\t{fstype}\t{opts}\t{dump}\t{pass_}"


def _build_fstab(*entries: str, comments: tuple = ()) -> str:
    lines = list(comments) + [e + "\n" for e in entries]
    return "".join(lines)


def _make_grub_ctrl(mocker, *, boot_uuid: str, efi_uuid: str | None) -> _GrubBootControl:
    """Bypass ``__init__`` and attach a minimally-mocked ``boot_slots``.

    Used by the per-helper tests that need a controller with just enough
    state to dispatch into the helper under test.
    """
    ctrl = object.__new__(_GrubBootControl)
    boot_slots = mocker.MagicMock()
    boot_slots.boot_partition.uuid = boot_uuid
    if efi_uuid is not None:
        boot_slots.efi_partition.uuid = efi_uuid
    else:
        boot_slots.efi_partition = None
    ctrl.boot_slots = boot_slots
    return ctrl


def _make_partial_grub_ctrl(
    mocker, *, current_slot: OTASlotBootID | None = None
) -> _GrubBootControl:
    """Like ``_make_grub_ctrl`` but only fills in ``current_slot`` for the
    boot-slot-dir tests."""
    ctrl = object.__new__(_GrubBootControl)
    boot_slots = mocker.MagicMock()
    if current_slot is not None:
        boot_slots.current_slot = current_slot
    ctrl.boot_slots = boot_slots
    return ctrl


def _patch_boot_dpath(mocker, boot_dir: Path) -> None:
    """Point ``boot_cfg.BOOT_DPATH`` at a tmp dir for the duration of a test."""
    mocker.patch(
        "otaclient.boot_control._grub_new.boot_cfg.BOOT_DPATH",
        str(boot_dir),
    )


#
# ------------ OTA status file migration ------------ #
#
# Case 2 (MIGRATE_FROM_OLD).


class TestMigrateOldGrubStatus:
    """Verify ``_migrate_status_files_from_old_grub_control`` carries old → new
    in case 2.

    Exercises the full ``GrubBootController.__init__`` so the migration is
    observed through ``get_booted_ota_status()`` — the surface an OTA-flow
    caller sees.
    """

    @pytest.fixture(autouse=True)
    def setup_env(
        self, boot_dir, rootfs_dir, mnt_dir, mock_grub_commands, redirect_paths
    ):
        """Activate path redirection / mocks."""

    def test_status_success_carries_over(self, boot_dir: Path):
        """Old grub recorded SUCCESS → new controller reports SUCCESS, not INITIALIZED."""
        _setup_old_grub_managed_boot(
            boot_dir, status="SUCCESS", slot_in_use="sda3", version="1.0.0"
        )
        controller = GrubBootController()

        assert controller._boot_control.resetup_requested is False
        assert controller.get_booted_ota_status() == OTAStatus.SUCCESS

        slot_a_dir = boot_dir / "ota-slot_a"
        assert (slot_a_dir / "status").read_text() == "SUCCESS"
        assert (slot_a_dir / "slot_in_use").read_text() == "ota-slot_a"
        assert (slot_a_dir / "version").read_text() == "1.0.0"

    def test_status_failure_carries_over(self, boot_dir: Path):
        """Old grub recorded FAILURE → new controller reports FAILURE."""
        _setup_old_grub_managed_boot(
            boot_dir, status="FAILURE", slot_in_use="sda3", version="1.0.0"
        )
        controller = GrubBootController()
        assert controller.get_booted_ota_status() == OTAStatus.FAILURE

    def test_slot_in_use_translation_for_slot_b(self, boot_dir: Path, mocker):
        """When old grub recorded slot_in_use=sda4 (= our slot_b), translation
        writes ``"ota-slot_b"``. We verify the translation independently of
        the boot-side switching-boot detection."""
        # current_slot is slot_a per conftest.ab_partition; old grub stored
        # slot_in_use=sda4 (an unusual but legal state, e.g. a partial OTA).
        _setup_old_grub_managed_boot(
            boot_dir, status="SUCCESS", slot_in_use="sda4", version="1.0.0"
        )

        controller = GrubBootController()
        slot_a_dir = boot_dir / "ota-slot_a"
        assert (slot_a_dir / "slot_in_use").read_text() == "ota-slot_b"
        assert controller._boot_control.resetup_requested is False

    def test_missing_old_status_files_skipped(self, boot_dir: Path):
        """Old grub layout with no status files: migration silently skips them.

        After migration the new slot folder has no status / version /
        slot_in_use, so ``OTAStatusFilesControl`` (which runs after
        ``_GrubBootControl.__init__``) seeds defaults — ``INITIALIZED``
        for status and ``current_slot`` for slot_in_use — through its own
        missing-file fallback path.
        """
        _setup_old_grub_managed_boot(boot_dir)
        # remove the status files in the source legacy folder
        for fname in ("status", "version", "slot_in_use"):
            (boot_dir / "ota-partition.sda3" / fname).unlink()

        controller = GrubBootController()
        assert controller._boot_control.resetup_requested is False
        assert controller.get_booted_ota_status() == OTAStatus.INITIALIZED

    def test_migration_does_not_touch_legacy_folder(self, boot_dir: Path):
        """Case 2 must not modify the active legacy folder — Group B's
        bootstrap predicate depends on the kernel still being there."""
        _setup_old_grub_managed_boot(boot_dir)
        active_legacy = boot_dir / "ota-partition.sda3"
        legacy_files_before = {
            f.name for f in active_legacy.iterdir() if not f.name.startswith(".")
        }

        GrubBootController()

        legacy_files_after = {
            f.name for f in active_legacy.iterdir() if not f.name.startswith(".")
        }
        assert legacy_files_before == legacy_files_after, (
            "case 2 bootstrap must not add/remove entries in the legacy folder"
        )

    def test_status_and_version_are_hardlinked(self, boot_dir: Path):
        """``status`` and ``version`` are hardlinked from the old legacy
        folder into the new slot folder (no data copy, single inode)."""
        _setup_old_grub_managed_boot(
            boot_dir, status="SUCCESS", slot_in_use="sda3", version="1.0.0"
        )

        GrubBootController()

        legacy_status = boot_dir / "ota-partition.sda3" / "status"
        new_status = boot_dir / "ota-slot_a" / "status"
        legacy_version = boot_dir / "ota-partition.sda3" / "version"
        new_version = boot_dir / "ota-slot_a" / "version"

        # status: still hardlinked at the moment of test (OTAStatusFilesControl
        # only writes if it needs to seed/update; a SUCCESS-status load is a
        # no-op write path, so the inode is preserved).
        assert legacy_status.stat().st_ino == new_status.stat().st_ino, (
            "migrated status must share an inode with the old legacy file"
        )
        assert legacy_version.stat().st_ino == new_version.stat().st_ino, (
            "migrated version must share an inode with the old legacy file"
        )
        # slot_in_use is NOT hardlinked: format differs (sda3 vs ota-slot_a),
        # so it's read+translated+atomically-written.
        legacy_slot_in_use = boot_dir / "ota-partition.sda3" / "slot_in_use"
        new_slot_in_use = boot_dir / "ota-slot_a" / "slot_in_use"
        assert legacy_slot_in_use.read_text() == "sda3"
        assert new_slot_in_use.read_text() == "ota-slot_a"
        assert legacy_slot_in_use.stat().st_ino != new_slot_in_use.stat().st_ino

    def test_version_detail_migrated(self, boot_dir: Path):
        """If the old legacy folder happens to carry a ``version_detail``
        file, it is migrated as well (forward-protective; old grub doesn't
        write this file in current versions)."""
        _setup_old_grub_managed_boot(
            boot_dir, status="SUCCESS", slot_in_use="sda3", version="1.0.0"
        )
        legacy = boot_dir / "ota-partition.sda3"
        (legacy / "version_detail").write_text(
            '{"release_name": "rel-1", "release_id": "id-1", "image_id": "img-1"}'
        )

        GrubBootController()

        new_version_detail = boot_dir / "ota-slot_a" / "version_detail"
        assert new_version_detail.is_file()
        assert (
            new_version_detail.stat().st_ino
            == (legacy / "version_detail").stat().st_ino
        ), "migrated version_detail must share an inode with the old legacy file"
        assert "rel-1" in new_version_detail.read_text()

    def test_migration_overwrites_pre_existing_dst(self, boot_dir: Path):
        """If a previous half-completed bootstrap left status files in the
        new slot folder, the migration replaces them with the legacy values
        (``remove_file(dst)`` before ``os.link``, so no ``FileExistsError``).
        """
        _setup_old_grub_managed_boot(
            boot_dir, status="SUCCESS", slot_in_use="sda3", version="1.0.0"
        )
        # Simulate stale dst from a previous half-attempt
        slot_a_dir = boot_dir / "ota-slot_a"
        slot_a_dir.mkdir(parents=True, exist_ok=True)
        (slot_a_dir / "status").write_text("LEFT_OVER")
        (slot_a_dir / "version").write_text("0.0.0-stale")

        GrubBootController()

        # Migration overwrote both with the legacy values
        assert (slot_a_dir / "status").read_text() == "SUCCESS"
        assert (slot_a_dir / "version").read_text() == "1.0.0"


#
# ------------ FRESH bootstrap mirror ------------ #
#
# Legacy folder mirror runs at the end of FRESH bootstrap.


class TestFreshBootstrapLegacyMirror:
    """Verify FRESH bootstrap mirrors active slot dir → legacy folder at end."""

    @pytest.fixture(autouse=True)
    def setup_env(
        self, boot_dir, rootfs_dir, mnt_dir, mock_grub_commands, redirect_paths
    ):
        """Activate path redirection / mocks."""

    def test_fresh_mirrors_kernel_and_initrd_to_active_legacy(self, boot_dir: Path):
        """After FRESH bootstrap, the active legacy folder contains the booted
        kernel and initrd as REAL FILES (so Group B's bootstrap predicate
        ``is_file(...)`` passes and Group B's ``_prepare_kernel_initrd_links``
        — which skips symlinks — picks them up during its OTA)."""
        _setup_fresh_grub_ecu_boot(boot_dir)

        GrubBootController()

        active_legacy = boot_dir / "ota-partition.sda3"
        assert active_legacy.is_dir(), "active legacy folder must be created"

        kernel = active_legacy / GRUB_KERNEL_FNAME
        initrd = active_legacy / GRUB_INITRD_FNAME
        assert kernel.is_file() and not kernel.is_symlink(), (
            "vmlinuz must be a real file (not a symlink) in the legacy folder"
        )
        assert initrd.is_file() and not initrd.is_symlink(), (
            "initrd must be a real file (not a symlink) in the legacy folder"
        )

    def test_fresh_does_not_create_standby_legacy(self, boot_dir: Path):
        """Standby legacy folder is intentionally NOT created on FRESH bootstrap.

        See plan §4: old grub's own ``__init__`` will ``mkdir(exist_ok=True)``
        it later if needed; pre-creating it would just leave an empty dir.
        """
        _setup_fresh_grub_ecu_boot(boot_dir)
        GrubBootController()
        assert not (boot_dir / "ota-partition.sda4").exists(), (
            "standby legacy folder must NOT be pre-created on FRESH bootstrap"
        )

    def test_fresh_mirror_prunes_unrelated_legacy_entries(self, boot_dir: Path):
        """Stale entries in the legacy folder whose names aren't in the source
        slot dir are pruned by the mirror (plan §3.2 step 3)."""
        _setup_fresh_grub_ecu_boot(boot_dir)

        # Pre-create the legacy folder with a stale file that does NOT
        # appear in /boot/ota-slot_a/. The mirror should remove it.
        stale_legacy = boot_dir / "ota-partition.sda3"
        stale_legacy.mkdir(parents=True)
        (stale_legacy / "leftover-from-old-install").write_text("stale")

        GrubBootController()

        assert not (stale_legacy / "leftover-from-old-install").exists(), (
            "stale legacy entry not in source slot dir must be pruned"
        )
        # but the mirrored kernel must be there
        assert (stale_legacy / GRUB_KERNEL_FNAME).is_file()

    def test_fresh_mirror_uses_hardlinks(self, boot_dir: Path):
        """Mirrored kernel/initrd share an inode with the source files in
        ``/boot/ota-slot_<active>/`` (real-file hardlink, not a copy)."""
        _setup_fresh_grub_ecu_boot(boot_dir)

        GrubBootController()

        src_slot_dir = boot_dir / "ota-slot_a"
        legacy = boot_dir / "ota-partition.sda3"
        for fname in (GRUB_KERNEL_FNAME, GRUB_INITRD_FNAME):
            src = src_slot_dir / fname
            dst = legacy / fname
            assert src.is_file() and dst.is_file()
            assert not dst.is_symlink(), (
                f"{fname} in legacy must be a real file (or hardlink), not a symlink"
            )
            assert src.stat().st_ino == dst.stat().st_ino, (
                f"{fname} must be hardlinked between new slot dir and legacy folder"
            )

    def test_fresh_mirror_overwrites_pre_existing_dst_files(self, boot_dir: Path):
        """A pre-existing real file at the legacy destination is replaced by
        the hardlink (the mirror does ``remove_file(dst)`` before
        ``os.link``, so no ``FileExistsError``)."""
        _setup_fresh_grub_ecu_boot(boot_dir)

        # Pre-seed legacy with stale same-name files that the mirror should overwrite
        stale_legacy = boot_dir / "ota-partition.sda3"
        stale_legacy.mkdir(parents=True)
        (stale_legacy / GRUB_KERNEL_FNAME).write_text("stale-kernel-content")
        (stale_legacy / GRUB_INITRD_FNAME).write_text("stale-initrd-content")

        GrubBootController()

        # Both legacy entries now hardlink-share with the source
        src_slot_dir = boot_dir / "ota-slot_a"
        for fname in (GRUB_KERNEL_FNAME, GRUB_INITRD_FNAME):
            assert (stale_legacy / fname).stat().st_ino == (
                src_slot_dir / fname
            ).stat().st_ino

    # ---- explicit cross-version predicate invariants ----

    def test_fresh_satisfies_group_b_bootstrap_predicate(self, boot_dir: Path):
        """After FRESH bootstrap, the on-disk state satisfies Group B's
        bootstrap predicate as a single composite check, so a Group B old
        controller running on this system would skip its own bootstrap.

        Mirrors Group B's ``_check_active_slot_ota_partition_file()`` per
        the design doc (§6, Group B column):

            is_file(/boot/ota-partition.sda<active_pid>/vmlinuz-<uname-r>)
            AND
            is_file(/boot/ota-partition.sda<active_pid>/initrd.img-<uname-r>)
        """
        _setup_fresh_grub_ecu_boot(boot_dir)
        GrubBootController()

        # current_slot=slot_a → active_pid=3 in the conftest UEFI layout.
        legacy_active = boot_dir / "ota-partition.sda3"
        # Group B predicate: AND of two is_file() checks. is_file() follows
        # symlinks, but our mirror plants real-file hardlinks, which also
        # satisfy any non-symlink filter old grub may apply later.
        assert (legacy_active / GRUB_KERNEL_FNAME).is_file(), (
            "Group B predicate: vmlinuz-<uname-r> must exist in active legacy folder"
        )
        assert (legacy_active / GRUB_INITRD_FNAME).is_file(), (
            "Group B predicate: initrd.img-<uname-r> must exist in active legacy folder"
        )

    def test_fresh_does_not_satisfy_group_a_bootstrap_predicate(self, boot_dir: Path):
        """After FRESH bootstrap, the artifacts that Group A's predicate
        looks for are intentionally absent — Group A's bootstrap will fire
        unconditionally on a new-managed system (per plan §2.4).

        Mirrors Group A's ``_check_active_slot_ota_partition_file()`` per
        the design doc (§6, Group A column). Group A's predicate is an OR
        of three signals; ANY one firing triggers bootstrap. We assert all
        three would fire so it's clear this is by design, not accidental.
        """
        _setup_fresh_grub_ecu_boot(boot_dir)
        GrubBootController()

        legacy_active = boot_dir / "ota-partition.sda3"
        # Signal 1: slot-folder vmlinuz-ota / initrd.img-ota symlinks
        # (the LITERAL symlink names — Group B uses version-named real files
        # instead, which we DO have, but Group A checks the symlink names).
        assert not (legacy_active / "vmlinuz-ota").exists(), (
            "Group A signal: vmlinuz-ota symlink must NOT exist (we don't "
            "create the symlink farm)"
        )
        assert not (legacy_active / "initrd.img-ota").exists(), (
            "Group A signal: initrd.img-ota symlink must NOT exist"
        )
        # Signal 3: /boot/ota-partition top-level symlink
        assert not (boot_dir / "ota-partition").is_symlink(), (
            "Group A signal: /boot/ota-partition top-level symlink must NOT exist"
        )
        # Signal 2 (BOOT_IMAGE basename ∈ {vmlinuz-ota, vmlinuz-ota.standby})
        # cannot be asserted from on-disk state alone — the cmdline is set
        # by GRUB at boot time. New grub's menuentry rewrites the linux line
        # to /ota-slot_<a/b>/vmlinuz-<ver>, so BOOT_IMAGE basename is
        # vmlinuz-<ver>, not vmlinuz-ota. This is enforced by
        # _BootMenuEntry._fixup_menuentry / _fixup_fpath in src; covered by
        # the menuentry unit tests in test_grub_new.py.


#
# ------------ Post-update legacy wipe ------------ #
#
# Standby legacy folder wipe in OTA post-update.


class TestPostUpdateLegacyWipe:
    """Verify the standby legacy folder is wiped during OTA post-update.

    The wipe is invoked from ``_post_update_platform_specific`` (right after
    ``setup_boot_slot_dir`` populates the new slot's boot dir), see plan §3.6.
    """

    @pytest.fixture(autouse=True)
    def setup_env(
        self, boot_dir, rootfs_dir, mnt_dir, mock_grub_commands, redirect_paths
    ):
        """Activate path redirection / mocks."""

    def _setup_standby_for_post_update(
        self, mnt_dir: Path, rootfs_dir: Path, *, kernel_ver: str = GRUB_NEW_KERNEL_VERSION
    ) -> None:
        """Synthesise a standby rootfs with a new kernel installed."""
        standby_mp = mnt_dir / "standby"
        standby_boot = standby_mp / "boot"
        standby_boot.mkdir(parents=True, exist_ok=True)
        (standby_boot / f"vmlinuz-{kernel_ver}").write_text("new-kernel")
        (standby_boot / f"initrd.img-{kernel_ver}").write_text("new-initrd")
        _create_grub_rootfs(standby_mp)

        active_mp = mnt_dir / "active"
        (active_mp / "etc").mkdir(parents=True, exist_ok=True)
        (active_mp / "etc" / "fstab").write_text(GRUB_SAMPLE_FSTAB)

    def test_wipe_legacy_compat_for_slot_removes_recursively(self, boot_dir: Path):
        """``_wipe_legacy_compat_for_slot(slot_b)`` deletes
        ``/boot/ota-partition.sda4/`` and all of its contents (real files,
        symlinks, nested dirs)."""
        _setup_new_grub_managed_boot(boot_dir)

        # Pre-create a stale standby legacy folder with mixed content.
        stale_legacy = boot_dir / "ota-partition.sda4"
        stale_legacy.mkdir(parents=True)
        (stale_legacy / GRUB_KERNEL_FNAME).write_text("stale-kernel")
        (stale_legacy / "status").write_text("SUCCESS")
        (stale_legacy / "vmlinuz-ota").symlink_to(GRUB_KERNEL_FNAME)
        (stale_legacy / "nested").mkdir()
        (stale_legacy / "nested" / "file").write_text("nested")

        controller = GrubBootController()
        controller._boot_control._wipe_legacy_compat_for_slot(OTASlotBootID.slot_b)

        assert not stale_legacy.exists(), (
            "standby legacy folder must be wiped recursively"
        )

    def test_wipe_legacy_compat_for_slot_no_op_when_absent(self, boot_dir: Path):
        """Wiping a non-existent legacy folder must not raise."""
        _setup_new_grub_managed_boot(boot_dir)
        assert not (boot_dir / "ota-partition.sda4").exists()

        controller = GrubBootController()
        # Must complete without raising.
        controller._boot_control._wipe_legacy_compat_for_slot(OTASlotBootID.slot_b)
        assert not (boot_dir / "ota-partition.sda4").exists()

    def test_post_update_e2e_wipes_standby_legacy(
        self, boot_dir: Path, mnt_dir: Path, rootfs_dir: Path, mocker: MockerFixture
    ):
        """End-to-end: a full ``post_update`` run wipes the standby legacy
        folder regardless of whether it was pre-existing."""
        _setup_new_grub_managed_boot(boot_dir)

        # Pre-create the standby legacy folder with a stale kernel. This
        # represents the worst case for the OTA-to-old-otaclient scenario:
        # if we didn't wipe, an installed Group B old otaclient would skip
        # its bootstrap on first boot.
        stale_legacy = boot_dir / "ota-partition.sda4"
        stale_legacy.mkdir(parents=True)
        (stale_legacy / GRUB_KERNEL_FNAME).write_text("stale")

        # pre_update_platform_specific cleans the new slot dir; mimic it
        slot_b_dir = boot_dir / "ota-slot_b"
        slot_b_dir.mkdir(parents=True, exist_ok=True)
        (slot_b_dir / "grub").mkdir(exist_ok=True)
        (boot_dir / "ota-slot_a" / "status").write_text("FAILURE")
        (boot_dir / "ota-slot_a" / "slot_in_use").write_text("ota-slot_b")

        self._setup_standby_for_post_update(
            mnt_dir, rootfs_dir, kernel_ver=GRUB_KERNEL_VERSION
        )
        # Standby OTA config files (post_update would otherwise abort)
        ota_dir = mnt_dir / "standby" / "boot" / "ota"
        ota_dir.mkdir(parents=True, exist_ok=True)
        (ota_dir / "proxy_info.yaml").write_text("new-proxy-config")
        (ota_dir / "ecu_info.yaml").write_text("new-ecu-config")

        controller = GrubBootController()
        controller._mp_control.prepare_standby_dev = mocker.MagicMock()
        controller._mp_control.mount_standby = mocker.MagicMock()
        controller._mp_control.mount_active = mocker.MagicMock()
        controller._mp_control.umount_all = mocker.MagicMock()

        controller.post_update(update_version="3.0.0")

        assert not stale_legacy.exists(), (
            "standby legacy folder must be wiped at the end of post_update"
        )
        # Active legacy is NOT touched by post_update (we only wipe standby's).
        # _setup_new_grub_managed_boot doesn't pre-create active legacy; the
        # mirror runs only at FRESH bootstrap, not on ALREADY_NEW.
        assert not (boot_dir / "ota-partition.sda3").exists()


#
# ------------ Group A bootstrap inputs ------------ #
#
# Bootstrap-path requirements for a Group A old controller (goal 4).


class TestGroupABootstrapInputs:
    """Verify the new controller leaves the artifacts a Group A old controller
    needs in order to bootstrap successfully on a new-managed system.

    Group A's bootstrap path requires real-file ``/boot/<vmlinuz-X>`` and
    ``/boot/<initrd.img-X>`` for the running kernel (its
    ``_get_current_booted_files()`` checks ``is_file(/boot/initrd.img-<ver>)``,
    and its ``shutil.copy`` step then reads ``/boot/<vmlinuz-X>``).
    See plan §2.4 for the trace.
    """

    @pytest.fixture(autouse=True)
    def setup_env(
        self, boot_dir, rootfs_dir, mnt_dir, mock_grub_commands, redirect_paths
    ):
        """Activate path redirection / mocks."""

    def test_fresh_bootstrap_keeps_kernel_and_initrd_at_boot_root(self, boot_dir: Path):
        """After FRESH bootstrap, ``/boot/<vmlinuz-X>`` and
        ``/boot/<initrd.img-X>`` are present as real files."""
        _setup_fresh_grub_ecu_boot(boot_dir)
        GrubBootController()

        kernel = boot_dir / GRUB_KERNEL_FNAME
        initrd = boot_dir / GRUB_INITRD_FNAME
        assert kernel.is_file() and not kernel.is_symlink()
        assert initrd.is_file() and not initrd.is_symlink()

    def test_post_update_keeps_active_and_standby_kernels_at_boot_root(
        self, boot_dir: Path, mnt_dir: Path, rootfs_dir: Path, mocker: MockerFixture
    ):
        """After OTA post-update, ``/boot/`` carries real-file kernels for
        both the active and the new standby kernel versions — so the post-
        reboot Group A old otaclient (running with the new kernel) finds
        ``/boot/<vmlinuz-NEW>`` for its bootstrap copy step."""
        _setup_new_grub_managed_boot(boot_dir)
        # Mimic pre_update side-effects
        slot_b_dir = boot_dir / "ota-slot_b"
        slot_b_dir.mkdir(parents=True, exist_ok=True)
        (slot_b_dir / "grub").mkdir(exist_ok=True)
        (boot_dir / "ota-slot_a" / "status").write_text("FAILURE")
        (boot_dir / "ota-slot_a" / "slot_in_use").write_text("ota-slot_b")

        # Standby rootfs ships a new kernel (different version → bypass
        # menuentry generation by mocking setup_ota_boot_cfg_for_slot,
        # following the same pattern as test_post_update_cleans_old_boot_files
        # in test_grub_boot_controller.py).
        standby_mp = mnt_dir / "standby"
        standby_boot = standby_mp / "boot"
        standby_boot.mkdir(parents=True, exist_ok=True)
        (standby_boot / GRUB_NEW_KERNEL_FNAME).write_text("new-kernel")
        (standby_boot / GRUB_NEW_INITRD_FNAME).write_text("new-initrd")
        _create_grub_rootfs(standby_mp)

        active_mp = mnt_dir / "active"
        (active_mp / "etc").mkdir(parents=True, exist_ok=True)
        (active_mp / "etc" / "fstab").write_text(GRUB_SAMPLE_FSTAB)
        ota_dir = standby_mp / "boot" / "ota"
        ota_dir.mkdir(parents=True, exist_ok=True)
        (ota_dir / "proxy_info.yaml").write_text("new-proxy-config")
        (ota_dir / "ecu_info.yaml").write_text("new-ecu-config")

        controller = GrubBootController()
        controller._mp_control.prepare_standby_dev = mocker.MagicMock()
        controller._mp_control.mount_standby = mocker.MagicMock()
        controller._mp_control.mount_active = mocker.MagicMock()
        controller._mp_control.umount_all = mocker.MagicMock()
        # The mocked _grub_mkconfig_on_mp returns a static template carrying
        # only GRUB_KERNEL_VERSION; bypass the per-slot boot cfg generation so the
        # post_update completes for the cross-version case.
        controller._boot_control.setup_ota_boot_cfg_for_slot = mocker.MagicMock()

        controller.post_update(update_version="3.0.0")

        # Active kernel preserved (running kernel won't change until reboot)
        assert (boot_dir / GRUB_KERNEL_FNAME).is_file()
        assert (boot_dir / GRUB_INITRD_FNAME).is_file()
        # New standby kernel populated for backward compat
        assert (boot_dir / GRUB_NEW_KERNEL_FNAME).is_file()
        assert (boot_dir / GRUB_NEW_INITRD_FNAME).is_file()


#
# ------------ Migrate + OTA full cycle ------------ #
#
# MIGRATE_FROM_OLD followed by a subsequent new-grub OTA.


class TestMigrateThenOTAFullCycle:
    """Walk plan §6.2's case-2 lifecycle: an old-grub-managed system is
    migrated by the new controller (status carryover + bootstrap), then a
    subsequent new-grub OTA runs against it.

    Validates that the migrated state survives the OTA flow and that the
    OTA's wipe of the standby legacy folder still applies.
    """

    @pytest.fixture(autouse=True)
    def setup_env(
        self, boot_dir, rootfs_dir, mnt_dir, mock_grub_commands, redirect_paths
    ):
        """Activate path redirection / mocks."""

    def test_migrate_then_post_update(
        self, boot_dir: Path, mnt_dir: Path, rootfs_dir: Path, mocker: MockerFixture
    ):
        """case 2 → run an OTA → verify post-state.

        Steps mirror plan §6.2 ("Case 2 (MIGRATE_FROM_OLD) lifecycle"):

        1. Old-grub-managed boot, status=SUCCESS at slot_a (sda3).
        2. New ``GrubBootController()`` triggers MIGRATE_FROM_OLD bootstrap;
           status carries over (SUCCESS), legacy folder untouched.
        3. Pre-update mimic: standby slot dir cleaned, active marked FAILURE.
        4. ``post_update`` runs:
           - standby ``/boot/ota-slot_b/`` populated with new kernel.
           - standby legacy ``/boot/ota-partition.sda4/`` wiped.
           - active legacy ``/boot/ota-partition.sda3/`` left untouched
             (it's not the slot being updated).
           - ``/boot/`` kernel copies updated for active and standby versions.
        """
        # --- Stage 1: old-grub-managed system, slot_a is active, SUCCESS state.
        _setup_old_grub_managed_boot(
            boot_dir, status="SUCCESS", slot_in_use="sda3", version="1.0.0"
        )
        active_legacy = boot_dir / "ota-partition.sda3"
        standby_legacy = boot_dir / "ota-partition.sda4"
        active_legacy_files_before = {
            f.name for f in active_legacy.iterdir() if not f.name.startswith(".")
        }

        # --- Stage 2: new controller takes over via MIGRATE_FROM_OLD bootstrap.
        controller = GrubBootController()
        assert controller._boot_control.resetup_requested is False
        assert controller.get_booted_ota_status() == OTAStatus.SUCCESS
        slot_a_dir = boot_dir / "ota-slot_a"
        assert (slot_a_dir / "status").read_text() == "SUCCESS"
        assert (slot_a_dir / "slot_in_use").read_text() == "ota-slot_a"
        assert (slot_a_dir / "version").read_text() == "1.0.0"
        # Active legacy folder is NOT touched by case-2 bootstrap.
        active_legacy_files_after_migrate = {
            f.name for f in active_legacy.iterdir() if not f.name.startswith(".")
        }
        assert active_legacy_files_before == active_legacy_files_after_migrate

        # --- Stage 3: mimic the pre_update side-effects on the live controller.
        slot_b_dir = boot_dir / "ota-slot_b"
        slot_b_dir.mkdir(parents=True, exist_ok=True)
        (slot_b_dir / "grub").mkdir(exist_ok=True)
        (slot_a_dir / "status").write_text("FAILURE")
        (slot_a_dir / "slot_in_use").write_text("ota-slot_b")
        # Standby rootfs ships the same kernel version (so the mocked
        # grub-mkconfig template still finds a matching menuentry).
        standby_mp = mnt_dir / "standby"
        standby_boot = standby_mp / "boot"
        standby_boot.mkdir(parents=True, exist_ok=True)
        (standby_boot / GRUB_KERNEL_FNAME).write_text("new-kernel")
        (standby_boot / GRUB_INITRD_FNAME).write_text("new-initrd")
        _create_grub_rootfs(standby_mp)
        active_mp = mnt_dir / "active"
        (active_mp / "etc").mkdir(parents=True, exist_ok=True)
        (active_mp / "etc" / "fstab").write_text(GRUB_SAMPLE_FSTAB)
        ota_dir = standby_mp / "boot" / "ota"
        ota_dir.mkdir(parents=True, exist_ok=True)
        (ota_dir / "proxy_info.yaml").write_text("new-proxy-config")
        (ota_dir / "ecu_info.yaml").write_text("new-ecu-config")

        controller._mp_control.prepare_standby_dev = mocker.MagicMock()
        controller._mp_control.mount_standby = mocker.MagicMock()
        controller._mp_control.mount_active = mocker.MagicMock()
        controller._mp_control.umount_all = mocker.MagicMock()

        # --- Stage 4: run the OTA.
        controller.post_update(update_version="3.0.0")

        # Standby slot dir populated with new-image kernel.
        assert (slot_b_dir / GRUB_KERNEL_FNAME).is_file()
        assert (slot_b_dir / GRUB_INITRD_FNAME).is_file()
        # Standby OTA-status files set by post_update_standby.
        assert (slot_b_dir / "status").read_text() == "UPDATING"
        assert (slot_b_dir / "version").read_text() == "3.0.0"
        assert (slot_b_dir / "slot_in_use").read_text() == "ota-slot_b"
        # Standby legacy folder wiped (so an old-otaclient image installed
        # via this OTA bootstraps fresh on first boot).
        assert not standby_legacy.exists(), (
            "standby legacy folder must be wiped at the end of post_update "
            "even after a MIGRATE_FROM_OLD bootstrap"
        )
        # Active legacy folder still untouched: it carries the old-grub-era
        # state, which is what Group B's bootstrap predicate continues to
        # depend on if a Group B old controller runs again on this slot.
        active_legacy_files_after_ota = {
            f.name for f in active_legacy.iterdir() if not f.name.startswith(".")
        }
        assert active_legacy_files_before == active_legacy_files_after_ota
        # /boot/ kernel copies present for active + standby (Group A precondition).
        assert (boot_dir / GRUB_KERNEL_FNAME).is_file()
        assert (boot_dir / GRUB_INITRD_FNAME).is_file()


#
# ------------ Per-helper tests ------------ #
#
# These tests scope to one ``_GrubBootControl`` helper at a time using a
# partially-mocked controller, but still exercise real filesystem I/O.
# They regression-test specific code paths that the end-to-end tests
# above cover only indirectly.


#
# ------------ Bootstrap setup rootfs ------------ #
#
# ``_GrubBootControl._bootstrap_setup_rootfs_for_ota_boot``


class TestBootstrapSetupRootfsForOtaBoot:
    """Bootstrap-time fstab rewrite must preserve the live system's mount
    options for ``/``, ``/boot``, ``/boot/efi``.

    Bootstrap has no sibling slot to reference; the slot being rewritten IS
    the active slot, so its own fstab must serve as the reference for the
    special mounts (otherwise the call site's missing ``reference_fstab``
    triggers ``_generate_fstab``'s hardcoded ext4/defaults fallback and
    silently drops user options like ``noatime`` / ``umask=0077``).
    """

    _ACTIVE_FSUUID = "active-slot-uuid"
    _BOOT_UUID = "boot-part-uuid"
    _EFI_UUID = "efi-part-uuid"

    @staticmethod
    def _make_slot_mp(tmp_path: Path, fstab_text: str) -> Path:
        """Lay out a minimal slot rootfs with the three files bootstrap touches."""
        slot_mp = tmp_path / "slot_mp"
        (slot_mp / "etc" / "default").mkdir(parents=True)
        (slot_mp / "etc" / "grub.d").mkdir(parents=True)
        (slot_mp / "etc" / "fstab").write_text(fstab_text)
        (slot_mp / "etc" / "default" / "grub").write_text("# placeholder\n")
        return slot_mp

    @classmethod
    def _make_ctrl(cls, mocker) -> _GrubBootControl:
        """Build a controller with ``current_slot`` set so
        ``get_slot_info(current_slot)`` resolves to a slot of known uuid."""
        ctrl = _make_grub_ctrl(mocker, boot_uuid=cls._BOOT_UUID, efi_uuid=cls._EFI_UUID)
        ctrl.boot_slots.current_slot = OTASlotBootID.slot_a
        ctrl.boot_slots.slot_a.uuid = cls._ACTIVE_FSUUID
        return ctrl

    def test_preserves_custom_mount_options_for_special_mounts(
        self, tmp_path: Path, mocker
    ):
        """Load-bearing regression: prior to the fix, the rewritten fstab
        replaced ``/`` / ``/boot`` / ``/boot/efi`` with hardcoded
        ``errors=remount-ro`` / ``defaults`` and silently dropped user options.
        """
        custom_root = _fstab_entry(
            self._ACTIVE_FSUUID, "/", "ext4", "errors=remount-ro,noatime", "0", "1"
        )
        custom_boot = _fstab_entry(
            self._BOOT_UUID, "/boot", "ext4", "noatime,defaults", "0", "1"
        )
        custom_efi = _fstab_entry(
            self._EFI_UUID, "/boot/efi", "vfat", "umask=0077", "0", "1"
        )
        slot_mp = self._make_slot_mp(
            tmp_path, _build_fstab(custom_root, custom_boot, custom_efi)
        )
        ctrl = self._make_ctrl(mocker)

        ctrl._bootstrap_setup_rootfs_for_ota_boot(slot_mp)

        rewritten_lines = (slot_mp / "etc" / "fstab").read_text().strip().splitlines()
        # Three special-mount lines come first, in this order.
        assert rewritten_lines[0] == custom_root
        assert rewritten_lines[1] == custom_boot
        assert rewritten_lines[2] == custom_efi

    def test_does_not_emit_fallback_warning(
        self, tmp_path: Path, mocker, caplog: pytest.LogCaptureFixture
    ):
        """No "no root entry in reference fstab" warning when the live fstab
        has the special mounts."""
        slot_mp = self._make_slot_mp(
            tmp_path,
            _build_fstab(
                _fstab_entry(
                    self._ACTIVE_FSUUID, "/", "ext4", "errors=remount-ro", "0", "1"
                ),
                _fstab_entry(self._BOOT_UUID, "/boot", "ext4", "defaults", "0", "1"),
                _fstab_entry(self._EFI_UUID, "/boot/efi", "vfat", "defaults", "0", "1"),
            ),
        )
        ctrl = self._make_ctrl(mocker)

        ctrl._bootstrap_setup_rootfs_for_ota_boot(slot_mp)

        assert not any(
            "no root entry in reference fstab" in rec.message for rec in caplog.records
        ), "bootstrap should have used the live fstab as reference"

    def test_extra_mounts_carried_over(self, tmp_path: Path, mocker):
        """Non-special mounts (e.g. ``/data``) are still carried from base."""
        custom_root = _fstab_entry(
            self._ACTIVE_FSUUID, "/", "ext4", "errors=remount-ro", "0", "1"
        )
        custom_boot = _fstab_entry(
            self._BOOT_UUID, "/boot", "ext4", "defaults", "0", "1"
        )
        custom_efi = _fstab_entry(
            self._EFI_UUID, "/boot/efi", "vfat", "defaults", "0", "1"
        )
        data_entry = _fstab_entry(
            "data-part-uuid", "/data", "ext4", "defaults", "0", "2"
        )
        slot_mp = self._make_slot_mp(
            tmp_path, _build_fstab(custom_root, custom_boot, custom_efi, data_entry)
        )
        ctrl = self._make_ctrl(mocker)

        ctrl._bootstrap_setup_rootfs_for_ota_boot(slot_mp)

        rewritten = (slot_mp / "etc" / "fstab").read_text()
        assert data_entry in rewritten.splitlines()


#
# ------------ kernel/initrd hardlink at /boot root ------------ #
#
# Old grub's bootstrap predicate checks ``is_file`` at
# ``/boot/<vmlinuz-uname-r>``, so both ``_bootstrap_setup_boot_slot_dir``
# and ``setup_boot_slot_dir`` place the boot files under
# ``/boot/ota-slot_<id>/`` AND expose them at the ``/boot/`` root via a
# hardlink to the slot dir copy.
#
# Both ``_bootstrap_setup_boot_slot_dir`` and ``setup_boot_slot_dir`` must:
# - place the boot files under ``/boot/ota-slot_<id>/`` (real file)
# - expose them at the ``/boot/`` root via a hardlink to the slot dir copy
#   (the old grub controller's bootstrap predicate checks ``is_file`` at
#   ``/boot/<vmlinuz-uname-r>``).


class TestBootstrapSetupBootSlotDir:
    """``_GrubBootControl._bootstrap_setup_boot_slot_dir`` populates the
    slot dir AND hardlinks the boot files at ``/boot/`` root (same inode),
    so the old grub controller's bootstrap predicate passes without paying
    for two full copies on the boot fs.
    """

    _KERNEL_VER = "6.11.0-29-generic"

    @staticmethod
    def _make_src_files(src_dir: Path, kernel_ver: str) -> tuple[Path, Path]:
        src_dir.mkdir(parents=True, exist_ok=True)
        kernel = src_dir / f"{VMLINUZ_PREFIX}{kernel_ver}"
        initrd = src_dir / f"{INITRD_PREFIX}{kernel_ver}"
        kernel.write_bytes(b"kernel-content")
        initrd.write_bytes(b"initrd-content")
        return kernel, initrd

    def test_files_placed_in_slot_dir(self, tmp_path, mocker):
        """Slot dir gets a regular file copy of the source kernel/initrd."""
        slot_id = OTASlotBootID.slot_a
        _patch_boot_dpath(mocker, tmp_path)
        ctrl = _make_partial_grub_ctrl(mocker, current_slot=slot_id)

        # source: legacy /boot/ota-partition.sda3/ (case 3 in retrieve)
        src_kernel, src_initrd = self._make_src_files(
            tmp_path / "ota-partition.sda3", self._KERNEL_VER
        )
        ctrl._bootstrap_setup_boot_slot_dir(
            BootFiles(self._KERNEL_VER, src_kernel, src_initrd)
        )

        slot_boot_dir = tmp_path / slot_id
        assert (slot_boot_dir / src_kernel.name).read_bytes() == b"kernel-content"
        assert (slot_boot_dir / src_initrd.name).read_bytes() == b"initrd-content"

    def test_boot_root_files_hardlinked_to_slot_dir(self, tmp_path, mocker):
        """``/boot/<vmlinuz>`` and ``/boot/<initrd>`` are hardlinks to the
        slot dir copy."""
        slot_id = OTASlotBootID.slot_a
        _patch_boot_dpath(mocker, tmp_path)
        ctrl = _make_partial_grub_ctrl(mocker, current_slot=slot_id)

        src_kernel, src_initrd = self._make_src_files(
            tmp_path / "ota-partition.sda3", self._KERNEL_VER
        )
        ctrl._bootstrap_setup_boot_slot_dir(
            BootFiles(self._KERNEL_VER, src_kernel, src_initrd)
        )

        slot_boot_dir = tmp_path / slot_id
        for fname in (src_kernel.name, src_initrd.name):
            slot_path = slot_boot_dir / fname
            root_path = tmp_path / fname
            assert root_path.is_file() and not root_path.is_symlink()
            # same inode → hardlink
            assert slot_path.stat().st_ino == root_path.stat().st_ino

    def test_replaces_stale_boot_root_file(self, tmp_path, mocker):
        """A pre-existing ``/boot/<vmlinuz>`` from a prior bootstrap is
        replaced with a fresh hardlink to the new slot dir copy."""
        slot_id = OTASlotBootID.slot_a
        _patch_boot_dpath(mocker, tmp_path)
        ctrl = _make_partial_grub_ctrl(mocker, current_slot=slot_id)

        src_kernel, src_initrd = self._make_src_files(
            tmp_path / "ota-partition.sda3", self._KERNEL_VER
        )
        # Stale unrelated content already at /boot/<vmlinuz>.
        stale = tmp_path / src_kernel.name
        stale.write_bytes(b"stale-content")

        ctrl._bootstrap_setup_boot_slot_dir(
            BootFiles(self._KERNEL_VER, src_kernel, src_initrd)
        )

        slot_boot_dir = tmp_path / slot_id
        root_kernel = tmp_path / src_kernel.name
        assert root_kernel.read_bytes() == b"kernel-content"
        assert (
            slot_boot_dir / src_kernel.name
        ).stat().st_ino == root_kernel.stat().st_ino

    def test_source_already_in_slot_dir(self, tmp_path, mocker):
        """When the source already lives in the slot dir (broken-but-set-up
        system), the same file is hardlinked to ``/boot`` root with no
        intermediate copy."""
        slot_id = OTASlotBootID.slot_a
        _patch_boot_dpath(mocker, tmp_path)
        ctrl = _make_partial_grub_ctrl(mocker, current_slot=slot_id)

        slot_boot_dir = tmp_path / slot_id
        src_kernel, src_initrd = self._make_src_files(slot_boot_dir, self._KERNEL_VER)
        src_kernel_ino = src_kernel.stat().st_ino
        src_initrd_ino = src_initrd.stat().st_ino

        ctrl._bootstrap_setup_boot_slot_dir(
            BootFiles(self._KERNEL_VER, src_kernel, src_initrd)
        )

        # No re-copy: the slot dir file is the same inode as before.
        assert src_kernel.stat().st_ino == src_kernel_ino
        assert src_initrd.stat().st_ino == src_initrd_ino
        # /boot/<name> is a hardlink to the same inode.
        assert (tmp_path / src_kernel.name).stat().st_ino == src_kernel_ino
        assert (tmp_path / src_initrd.name).stat().st_ino == src_initrd_ino


class TestSetupBootSlotDir:
    """``_GrubBootControl.setup_boot_slot_dir`` (post-OTA path) copies
    kernel/initrd from the slot mount point into the slot's boot dir, and
    exposes them at ``/boot/`` root via hardlink."""

    _KERNEL_VER = "6.11.0-29-generic"

    @staticmethod
    def _setup_dirs(tmp_path: Path, slot_id: OTASlotBootID) -> tuple[Path, Path, Path]:
        boot_dir = tmp_path / "boot"
        slot_boot_dir = boot_dir / slot_id
        slot_mp = tmp_path / "slot_mp"
        slot_boot_dir.mkdir(parents=True)
        (slot_mp / "boot").mkdir(parents=True)
        return boot_dir, slot_boot_dir, slot_mp

    @staticmethod
    def _make_slot_files(slot_mp: Path, kernel_ver: str) -> tuple[Path, Path]:
        kernel = slot_mp / "boot" / f"{VMLINUZ_PREFIX}{kernel_ver}"
        initrd = slot_mp / "boot" / f"{INITRD_PREFIX}{kernel_ver}"
        kernel.write_bytes(b"new-kernel")
        initrd.write_bytes(b"new-initrd")
        return kernel, initrd

    def test_files_placed_in_slot_dir(self, tmp_path, mocker):
        slot_id = OTASlotBootID.slot_b
        boot_dir, slot_boot_dir, slot_mp = self._setup_dirs(tmp_path, slot_id)
        _patch_boot_dpath(mocker, boot_dir)
        ctrl = _make_partial_grub_ctrl(mocker)
        kernel, initrd = self._make_slot_files(slot_mp, self._KERNEL_VER)

        ctrl.setup_boot_slot_dir(self._KERNEL_VER, slot_id=slot_id, slot_mp=slot_mp)

        assert (slot_boot_dir / kernel.name).read_bytes() == b"new-kernel"
        assert (slot_boot_dir / initrd.name).read_bytes() == b"new-initrd"

    def test_boot_root_files_hardlinked_to_slot_dir(self, tmp_path, mocker):
        slot_id = OTASlotBootID.slot_b
        boot_dir, slot_boot_dir, slot_mp = self._setup_dirs(tmp_path, slot_id)
        _patch_boot_dpath(mocker, boot_dir)
        ctrl = _make_partial_grub_ctrl(mocker)
        kernel, initrd = self._make_slot_files(slot_mp, self._KERNEL_VER)

        ctrl.setup_boot_slot_dir(self._KERNEL_VER, slot_id=slot_id, slot_mp=slot_mp)

        for fname in (kernel.name, initrd.name):
            slot_path = slot_boot_dir / fname
            root_path = boot_dir / fname
            assert root_path.is_file() and not root_path.is_symlink()
            assert slot_path.stat().st_ino == root_path.stat().st_ino

    def test_replaces_stale_boot_root_file(self, tmp_path, mocker):
        """A leftover ``/boot/<vmlinuz>`` from a previous OTA must be replaced
        with a hardlink to the new slot dir copy, not retain stale content."""
        slot_id = OTASlotBootID.slot_b
        boot_dir, slot_boot_dir, slot_mp = self._setup_dirs(tmp_path, slot_id)
        _patch_boot_dpath(mocker, boot_dir)
        ctrl = _make_partial_grub_ctrl(mocker)
        kernel, _ = self._make_slot_files(slot_mp, self._KERNEL_VER)
        (boot_dir / kernel.name).write_bytes(b"stale-kernel")

        ctrl.setup_boot_slot_dir(self._KERNEL_VER, slot_id=slot_id, slot_mp=slot_mp)

        assert (boot_dir / kernel.name).read_bytes() == b"new-kernel"
        assert (slot_boot_dir / kernel.name).stat().st_ino == (
            boot_dir / kernel.name
        ).stat().st_ino

    def test_skips_when_source_is_symlink(self, tmp_path, mocker):
        """If ``vmlinuz-<ver>`` in the slot mount point is itself a symlink
        (not a regular file), the loop skips it — no slot dir copy and no
        ``/boot`` root hardlink is produced."""
        slot_id = OTASlotBootID.slot_b
        boot_dir, slot_boot_dir, slot_mp = self._setup_dirs(tmp_path, slot_id)
        _patch_boot_dpath(mocker, boot_dir)
        ctrl = _make_partial_grub_ctrl(mocker)

        # real targets exist, but the kernel/initrd at the expected paths
        # are symlinks → must be skipped.
        real_kernel = slot_mp / "boot" / "vmlinuz-real"
        real_initrd = slot_mp / "boot" / "initrd-real"
        real_kernel.write_bytes(b"real-kernel")
        real_initrd.write_bytes(b"real-initrd")
        (slot_mp / "boot" / f"{VMLINUZ_PREFIX}{self._KERNEL_VER}").symlink_to(
            real_kernel
        )
        (slot_mp / "boot" / f"{INITRD_PREFIX}{self._KERNEL_VER}").symlink_to(
            real_initrd
        )

        ctrl.setup_boot_slot_dir(self._KERNEL_VER, slot_id=slot_id, slot_mp=slot_mp)

        assert not (slot_boot_dir / f"{VMLINUZ_PREFIX}{self._KERNEL_VER}").exists()
        assert not (slot_boot_dir / f"{INITRD_PREFIX}{self._KERNEL_VER}").exists()
        assert not (boot_dir / f"{VMLINUZ_PREFIX}{self._KERNEL_VER}").exists()
        assert not (boot_dir / f"{INITRD_PREFIX}{self._KERNEL_VER}").exists()


#
# ------------ Backward-compat helpers ------------ #
#
# Setup-case detection and ``slot_in_use`` translation.


class TestDetectBootControlSetupCase:
    """``_detect_boot_control_setup_case`` classifies the three layouts of
    ``boot_cfg.GRUB_CFG_FPATH`` (FRESH / MIGRATE_FROM_OLD / ALREADY_NEW)."""

    @pytest.fixture
    def grub_cfg_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Redirect ``boot_cfg.GRUB_CFG_FPATH`` to a path under ``tmp_path``."""
        target = tmp_path / "grub.cfg"
        monkeypatch.setattr(
            "otaclient.boot_control.configs.GrubControlNewConfig.GRUB_CFG_FPATH",
            str(target),
        )
        return target

    def test_fresh_when_grub_cfg_missing(self, grub_cfg_path: Path):
        """No grub.cfg at all → FRESH (will trigger bootstrap)."""
        assert not grub_cfg_path.exists()
        assert _detect_boot_control_setup_case() == _GrubBootControlSetupCase.FRESH

    def test_fresh_when_grub_cfg_unmanaged(self, grub_cfg_path: Path):
        """Plain grub.cfg without OTA-managed footer → FRESH."""
        grub_cfg_path.write_text(GRUB_CFG_CONTENT)
        assert _detect_boot_control_setup_case() == _GrubBootControlSetupCase.FRESH

    def test_fresh_when_grub_cfg_unreadable(self, grub_cfg_path: Path):
        """A grub.cfg that can't be read (e.g. permission denied) → FRESH.

        Logged warning + FRESH is the intended fallback.
        """
        grub_cfg_path.write_text(GRUB_CFG_CONTENT)
        from unittest.mock import patch

        with patch.object(Path, "read_text", side_effect=OSError("denied")):
            assert _detect_boot_control_setup_case() == _GrubBootControlSetupCase.FRESH

    def test_migrate_when_grub_cfg_is_symlink(self, grub_cfg_path: Path):
        """grub.cfg as a symlink → MIGRATE_FROM_OLD (only old grub maintains
        it as ``../ota-partition/grub.cfg``)."""
        grub_cfg_path.symlink_to("../ota-partition/grub.cfg")
        assert (
            _detect_boot_control_setup_case()
            == _GrubBootControlSetupCase.MIGRATE_FROM_OLD
        )

    def test_already_new_when_managed_footer_valid(self, grub_cfg_path: Path):
        """OTA-managed footer present and valid → ALREADY_NEW (no-op)."""
        managed = OTAManagedCfg(raw_contents=GRUB_CFG_CONTENT.strip(), grub_version="2.12")
        grub_cfg_path.write_text(managed.export())
        assert (
            _detect_boot_control_setup_case() == _GrubBootControlSetupCase.ALREADY_NEW
        )


class TestTranslateSlotInUseOldToNew:
    """``_translate_slot_in_use_old_to_new`` maps old-format ``"sda<pid>"`` to
    new-format ``OTASlotBootID``, falling back to ``current_slot`` on
    unrecognised input.

    Pure logic over ``boot_slots.old_slot_id_mapping``; we bypass ``__init__``
    via ``object.__new__`` and attach a minimal ``boot_slots`` mock.
    """

    @staticmethod
    def _make_ctrl(mocker, *, current_slot: OTASlotBootID = OTASlotBootID.slot_a):
        ctrl = object.__new__(_GrubBootControl)
        boot_slots = mocker.MagicMock()
        boot_slots.old_slot_id_mapping = {
            OTASlotBootID.slot_a: "ota-partition.sda3",
            OTASlotBootID.slot_b: "ota-partition.sda4",
        }
        boot_slots.current_slot = current_slot
        ctrl.boot_slots = boot_slots
        return ctrl

    def test_translate_slot_a(self, mocker):
        """``"sda3"`` → ``OTASlotBootID.slot_a`` (UEFI test layout)."""
        ctrl = self._make_ctrl(mocker)
        assert ctrl._translate_slot_in_use_old_to_new("sda3") == OTASlotBootID.slot_a

    def test_translate_slot_b(self, mocker):
        """``"sda4"`` → ``OTASlotBootID.slot_b`` (UEFI test layout)."""
        ctrl = self._make_ctrl(mocker)
        assert ctrl._translate_slot_in_use_old_to_new("sda4") == OTASlotBootID.slot_b

    def test_translate_returns_strenum_compatible_str(self, mocker):
        """Returned value is a ``StrEnum`` instance and is str-equal to the
        on-disk new-format string ``"ota-slot_<a/b>"``."""
        ctrl = self._make_ctrl(mocker)
        result = ctrl._translate_slot_in_use_old_to_new("sda3")
        assert isinstance(result, OTASlotBootID)
        assert result == "ota-slot_a"  # StrEnum: enum is str-equal to its value

    def test_translate_unknown_falls_back_to_current_slot(self, mocker, caplog):
        """Unrecognised input falls back to ``boot_slots.current_slot`` and logs."""
        ctrl = self._make_ctrl(mocker, current_slot=OTASlotBootID.slot_a)
        assert ctrl._translate_slot_in_use_old_to_new("sda9") == OTASlotBootID.slot_a
        assert any("unrecognised" in rec.message for rec in caplog.records), (
            "expected a warning log when input doesn't map to either slot"
        )

    def test_translate_with_full_legacy_folder_name_falls_back(self, mocker, caplog):
        """A value like ``"ota-partition.sda3"`` (the full legacy folder name)
        is NOT what old grub stores in ``slot_in_use``; it should not match."""
        ctrl = self._make_ctrl(mocker, current_slot=OTASlotBootID.slot_b)
        # Falls back to current_slot (slot_b in this scenario), not to slot_a.
        assert (
            ctrl._translate_slot_in_use_old_to_new("ota-partition.sda3")
            == OTASlotBootID.slot_b
        )
        assert any("unrecognised" in rec.message for rec in caplog.records)

    def test_translate_empty_string_falls_back(self, mocker, caplog):
        """Empty input → fallback. (The migrate helper guards against empty
        before calling, but this helper itself handles it gracefully.)"""
        ctrl = self._make_ctrl(mocker, current_slot=OTASlotBootID.slot_a)
        assert ctrl._translate_slot_in_use_old_to_new("") == OTASlotBootID.slot_a
        assert any("unrecognised" in rec.message for rec in caplog.records)
