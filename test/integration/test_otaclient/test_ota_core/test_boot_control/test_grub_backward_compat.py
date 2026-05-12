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
"""End-to-end backward-compatibility tests for the new grub boot controller.

Old-otaclient versions split by bootstrap predicate (PR
https://github.com/tier4/ota-client/pull/601):

- GROUP A (3.8.x~3.9.x): strict predicate, always bootstraps on a new-managed
  system. We only ensure its bootstrap PATH succeeds.
- GROUP B (3.10.x~3.13.x): relaxed predicate; we make it skip bootstrap.

Goal → test mapping:

1. Group B skips bootstrap on new-managed — ``TestFreshBootstrapLegacyMirror``,
   ``TestEnsureMirrorOnAlreadyNew``.
2. Group B can OTA new-managed (kernel as real-file hardlink) — same classes.
3. Status carryover on MIGRATE_FROM_OLD — ``TestMigrateOldGrubStatus``,
   ``TestMigrateThenOTAFullCycle``.
4. Group A's bootstrap PATH succeeds — ``TestGroupABootstrapInputs``.

Standby legacy wipe at OTA post-update — ``TestPostUpdateLegacyWipe``.
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

# ------------ Helpers ------------ #


def _fstab_entry(
    uuid: str, mp: str, fstype: str, opts: str, dump: str, pass_: str
) -> str:
    return f"UUID={uuid}\t{mp}\t{fstype}\t{opts}\t{dump}\t{pass_}"


def _build_fstab(*entries: str, comments: tuple = ()) -> str:
    return "".join(list(comments) + [e + "\n" for e in entries])


def _make_grub_ctrl(
    mocker, *, boot_uuid: str, efi_uuid: str | None
) -> _GrubBootControl:
    """Bypass ``__init__`` and attach a minimally-mocked ``boot_slots``."""
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
    ctrl = object.__new__(_GrubBootControl)
    boot_slots = mocker.MagicMock()
    if current_slot is not None:
        boot_slots.current_slot = current_slot
    ctrl.boot_slots = boot_slots
    return ctrl


def _patch_boot_dpath(mocker, boot_dir: Path) -> None:
    mocker.patch(
        "otaclient.boot_control._grub_new.boot_cfg.BOOT_DPATH",
        str(boot_dir),
    )


# ------------ Case 2: MIGRATE_FROM_OLD status carryover ------------ #


class TestMigrateOldGrubStatus:
    """``_migrate_from_old_grub_control`` carries old → new in case 2."""

    @pytest.fixture(autouse=True)
    def setup_env(
        self, boot_dir, rootfs_dir, mnt_dir, mock_grub_commands, redirect_paths
    ):
        """Setup the fixtures used by the tests."""

    @pytest.mark.parametrize(
        "status, expected",
        [("SUCCESS", OTAStatus.SUCCESS), ("FAILURE", OTAStatus.FAILURE)],
    )
    def test_status_carries_over(
        self, boot_dir: Path, status: str, expected: OTAStatus
    ):
        _setup_old_grub_managed_boot(
            boot_dir, status=status, slot_in_use="sda3", version="1.0.0"
        )
        controller = GrubBootController()

        assert controller._boot_control.resetup_requested is False
        assert controller.get_booted_ota_status() == expected

        slot_a_dir = boot_dir / "ota-slot_a"
        assert (slot_a_dir / "status").read_text() == status
        assert (slot_a_dir / "slot_in_use").read_text() == "ota-slot_a"
        assert (slot_a_dir / "version").read_text() == "1.0.0"

    def test_slot_in_use_translation_for_slot_b(self, boot_dir: Path):
        """Old grub recorded slot_in_use=sda4 → translated to ``ota-slot_b``."""
        _setup_old_grub_managed_boot(
            boot_dir, status="SUCCESS", slot_in_use="sda4", version="1.0.0"
        )
        GrubBootController()
        assert (boot_dir / "ota-slot_a" / "slot_in_use").read_text() == "ota-slot_b"

    def test_missing_old_status_files_skipped(self, boot_dir: Path):
        """Missing legacy status files → ``OTAStatusFilesControl`` seeds INITIALIZED."""
        _setup_old_grub_managed_boot(boot_dir)
        for fname in ("status", "version", "slot_in_use"):
            (boot_dir / "ota-partition.sda3" / fname).unlink()

        controller = GrubBootController()
        assert controller._boot_control.resetup_requested is False
        assert controller.get_booted_ota_status() == OTAStatus.INITIALIZED

    def test_migration_does_not_touch_legacy_folder(self, boot_dir: Path):
        """Group B's predicate depends on legacy kernel — case 2 must not touch it."""
        _setup_old_grub_managed_boot(boot_dir)
        active_legacy = boot_dir / "ota-partition.sda3"
        before = {f.name for f in active_legacy.iterdir() if not f.name.startswith(".")}

        GrubBootController()

        after = {f.name for f in active_legacy.iterdir() if not f.name.startswith(".")}
        assert before == after

    def test_status_and_version_are_hardlinked(self, boot_dir: Path):
        """``status`` / ``version`` share an inode with legacy; ``slot_in_use`` does not (format differs)."""
        _setup_old_grub_managed_boot(
            boot_dir, status="SUCCESS", slot_in_use="sda3", version="1.0.0"
        )
        GrubBootController()

        legacy = boot_dir / "ota-partition.sda3"
        new = boot_dir / "ota-slot_a"
        for fname in ("status", "version"):
            assert (legacy / fname).stat().st_ino == (new / fname).stat().st_ino

        assert (legacy / "slot_in_use").read_text() == "sda3"
        assert (new / "slot_in_use").read_text() == "ota-slot_a"
        assert (legacy / "slot_in_use").stat().st_ino != (
            new / "slot_in_use"
        ).stat().st_ino

    def test_version_detail_migrated(self, boot_dir: Path):
        """Forward-protective: if legacy carries ``version_detail``, it is hardlinked over."""
        _setup_old_grub_managed_boot(
            boot_dir, status="SUCCESS", slot_in_use="sda3", version="1.0.0"
        )
        legacy = boot_dir / "ota-partition.sda3"
        (legacy / "version_detail").write_text(
            '{"release_name": "rel-1", "release_id": "id-1", "image_id": "img-1"}'
        )

        GrubBootController()

        new_vd = boot_dir / "ota-slot_a" / "version_detail"
        assert new_vd.is_file()
        assert new_vd.stat().st_ino == (legacy / "version_detail").stat().st_ino
        assert "rel-1" in new_vd.read_text()

    def test_migration_overwrites_pre_existing_dst(self, boot_dir: Path):
        """``remove_file(dst)`` before ``os.link`` — half-completed bootstrap leftovers are replaced."""
        _setup_old_grub_managed_boot(
            boot_dir, status="SUCCESS", slot_in_use="sda3", version="1.0.0"
        )
        slot_a_dir = boot_dir / "ota-slot_a"
        slot_a_dir.mkdir(parents=True, exist_ok=True)
        (slot_a_dir / "status").write_text("LEFT_OVER")
        (slot_a_dir / "version").write_text("0.0.0-stale")

        GrubBootController()

        assert (slot_a_dir / "status").read_text() == "SUCCESS"
        assert (slot_a_dir / "version").read_text() == "1.0.0"


# ------------ FRESH bootstrap mirror ------------ #


class TestFreshBootstrapLegacyMirror:
    """Ensure-on-startup (§3.2) mirrors active slot dir → legacy on FRESH."""

    @pytest.fixture(autouse=True)
    def setup_env(
        self, boot_dir, rootfs_dir, mnt_dir, mock_grub_commands, redirect_paths
    ):
        pass

    def test_fresh_mirrors_kernel_and_initrd_to_active_legacy(self, boot_dir: Path):
        """After FRESH bootstrap, active legacy has kernel + initrd as real files."""
        _setup_fresh_grub_ecu_boot(boot_dir)
        GrubBootController()

        active_legacy = boot_dir / "ota-partition.sda3"
        assert active_legacy.is_dir()
        for fname in (GRUB_KERNEL_FNAME, GRUB_INITRD_FNAME):
            f = active_legacy / fname
            assert f.is_file() and not f.is_symlink()

    def test_fresh_does_not_create_standby_legacy(self, boot_dir: Path):
        """Standby legacy is intentionally NOT pre-created (plan §4)."""
        _setup_fresh_grub_ecu_boot(boot_dir)
        GrubBootController()
        assert not (boot_dir / "ota-partition.sda4").exists()

    def test_ensure_skips_when_legacy_folder_pre_existing(self, boot_dir: Path):
        """Skip-when-exists: leftover legacy content is preserved (plan §3.2)."""
        _setup_fresh_grub_ecu_boot(boot_dir)
        stale_legacy = boot_dir / "ota-partition.sda3"
        stale_legacy.mkdir(parents=True)
        (stale_legacy / "leftover-from-old-install").write_text("stale")

        GrubBootController()

        assert (stale_legacy / "leftover-from-old-install").read_text() == "stale"
        assert (boot_dir / "ota-slot_a" / GRUB_KERNEL_FNAME).is_file()
        assert not (stale_legacy / GRUB_KERNEL_FNAME).exists()

    def test_fresh_mirror_uses_hardlinks(self, boot_dir: Path):
        """Mirrored kernel/initrd share an inode with the source slot files."""
        _setup_fresh_grub_ecu_boot(boot_dir)
        GrubBootController()

        src = boot_dir / "ota-slot_a"
        legacy = boot_dir / "ota-partition.sda3"
        for fname in (GRUB_KERNEL_FNAME, GRUB_INITRD_FNAME):
            assert (legacy / fname).stat().st_ino == (src / fname).stat().st_ino
            assert not (legacy / fname).is_symlink()

    def test_fresh_mirror_captures_status_files(self, boot_dir: Path):
        """Ensure-step runs AFTER ``OTAStatusFilesControl`` — status files land in legacy.

        ``status`` is hardlinked; ``slot_in_use`` is translated new→old (atomic write).
        Load-bearing for the "AFTER OTAStatusFilesControl" placement.
        """
        _setup_fresh_grub_ecu_boot(boot_dir)
        GrubBootController()

        legacy = boot_dir / "ota-partition.sda3"
        slot_a = boot_dir / "ota-slot_a"

        assert (legacy / "status").stat().st_ino == (slot_a / "status").stat().st_ino
        assert (legacy / "status").read_text() == "INITIALIZED"

        assert (slot_a / "slot_in_use").read_text() == "ota-slot_a"
        assert (legacy / "slot_in_use").read_text() == "sda3"
        assert (legacy / "slot_in_use").stat().st_ino != (
            slot_a / "slot_in_use"
        ).stat().st_ino

    def test_fresh_does_not_satisfy_group_a_bootstrap_predicate(self, boot_dir: Path):
        """Group A's predicate signals are intentionally absent (plan §2.4).

        On-disk regression guard. The BOOT_IMAGE basename signal is covered
        by menuentry unit tests in ``test_grub_new.py`` — out of scope here.
        """
        _setup_fresh_grub_ecu_boot(boot_dir)
        GrubBootController()

        legacy_active = boot_dir / "ota-partition.sda3"
        assert not (legacy_active / "vmlinuz-ota").exists()
        assert not (legacy_active / "initrd.img-ota").exists()
        assert not (boot_dir / "ota-partition").is_symlink()


# ------------ Post-update legacy wipe ------------ #


class TestPostUpdateLegacyWipe:
    """Standby legacy folder is wiped at OTA post-update (plan §3.6)."""

    @pytest.fixture(autouse=True)
    def setup_env(
        self, boot_dir, rootfs_dir, mnt_dir, mock_grub_commands, redirect_paths
    ):
        """Setup fixtures for the tests."""

    def _setup_standby_for_post_update(
        self,
        mnt_dir: Path,
        rootfs_dir: Path,
        *,
        kernel_ver: str = GRUB_NEW_KERNEL_VERSION,
    ) -> None:
        standby_mp = mnt_dir / "standby"
        standby_boot = standby_mp / "boot"
        standby_boot.mkdir(parents=True, exist_ok=True)
        (standby_boot / f"vmlinuz-{kernel_ver}").write_text("new-kernel")
        (standby_boot / f"initrd.img-{kernel_ver}").write_text("new-initrd")
        _create_grub_rootfs(standby_mp)

        active_mp = mnt_dir / "active"
        (active_mp / "etc").mkdir(parents=True, exist_ok=True)
        (active_mp / "etc" / "fstab").write_text(GRUB_SAMPLE_FSTAB)

    def test_wipe_removes_recursively(self, boot_dir: Path):
        """``_wipe_legacy_compat_for_slot`` recursively deletes the legacy folder."""
        _setup_new_grub_managed_boot(boot_dir)

        stale_legacy = boot_dir / "ota-partition.sda4"
        stale_legacy.mkdir(parents=True)
        (stale_legacy / GRUB_KERNEL_FNAME).write_text("stale-kernel")
        (stale_legacy / "status").write_text("SUCCESS")
        (stale_legacy / "vmlinuz-ota").symlink_to(GRUB_KERNEL_FNAME)
        (stale_legacy / "nested").mkdir()
        (stale_legacy / "nested" / "file").write_text("nested")

        controller = GrubBootController()
        controller._boot_control._wipe_legacy_compat_for_slot(OTASlotBootID.slot_b)

        assert not stale_legacy.exists()

    def test_wipe_no_op_when_absent(self, boot_dir: Path):
        _setup_new_grub_managed_boot(boot_dir)
        assert not (boot_dir / "ota-partition.sda4").exists()

        controller = GrubBootController()
        controller._boot_control._wipe_legacy_compat_for_slot(OTASlotBootID.slot_b)
        assert not (boot_dir / "ota-partition.sda4").exists()

    def test_post_update_e2e_wipes_standby_legacy(
        self, boot_dir: Path, mnt_dir: Path, rootfs_dir: Path, mocker: MockerFixture
    ):
        """End-to-end: full ``post_update`` wipes the standby legacy regardless of pre-state."""
        _setup_new_grub_managed_boot(boot_dir)

        stale_legacy = boot_dir / "ota-partition.sda4"
        stale_legacy.mkdir(parents=True)
        (stale_legacy / GRUB_KERNEL_FNAME).write_text("stale")

        slot_b_dir = boot_dir / "ota-slot_b"
        slot_b_dir.mkdir(parents=True, exist_ok=True)
        (slot_b_dir / "grub").mkdir(exist_ok=True)
        (boot_dir / "ota-slot_a" / "status").write_text("FAILURE")
        (boot_dir / "ota-slot_a" / "slot_in_use").write_text("ota-slot_b")

        self._setup_standby_for_post_update(
            mnt_dir, rootfs_dir, kernel_ver=GRUB_KERNEL_VERSION
        )
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

        assert not stale_legacy.exists()
        # Active legacy survives — recreated by the ensure-step on startup,
        # untouched by post_update.
        active_legacy = boot_dir / "ota-partition.sda3"
        assert active_legacy.is_dir()
        assert (active_legacy / GRUB_KERNEL_FNAME).is_file()


# ------------ Ensure-step on ALREADY_NEW ------------ #


class TestEnsureMirrorOnAlreadyNew:
    """Ensure-step on ALREADY_NEW: mirror if missing, skip if present."""

    @pytest.fixture(autouse=True)
    def setup_env(
        self, boot_dir, rootfs_dir, mnt_dir, mock_grub_commands, redirect_paths
    ):
        """Setup fixtures for the tests."""

    def test_missing_legacy_is_mirrored_with_kernel_and_initrd(self, boot_dir: Path):
        """Post-OTA-reboot recovery: missing legacy is mirrored from new slot dir."""
        _setup_new_grub_managed_boot(boot_dir)
        assert not (boot_dir / "ota-partition.sda3").exists()

        GrubBootController()

        active_legacy = boot_dir / "ota-partition.sda3"
        slot_a = boot_dir / "ota-slot_a"
        assert active_legacy.is_dir()
        for fname in (GRUB_KERNEL_FNAME, GRUB_INITRD_FNAME):
            dst = active_legacy / fname
            assert dst.is_file() and not dst.is_symlink()
            assert dst.stat().st_ino == (slot_a / fname).stat().st_ino

    def test_missing_legacy_captures_status_files(self, boot_dir: Path):
        """Ensure-step (after ``OTAStatusFilesControl``) plants status into legacy.

        ``status`` / ``version`` hardlinked; ``slot_in_use`` translated new→old.
        """
        _setup_new_grub_managed_boot(
            boot_dir, ota_status="SUCCESS", slot_in_use="ota-slot_a"
        )
        GrubBootController()

        legacy = boot_dir / "ota-partition.sda3"
        slot_a = boot_dir / "ota-slot_a"
        for fname in ("status", "version"):
            assert (legacy / fname).stat().st_ino == (slot_a / fname).stat().st_ino
            assert (legacy / fname).read_text() == (slot_a / fname).read_text()

        assert (slot_a / "slot_in_use").read_text() == "ota-slot_a"
        assert (legacy / "slot_in_use").read_text() == "sda3"
        assert (legacy / "slot_in_use").stat().st_ino != (
            slot_a / "slot_in_use"
        ).stat().st_ino

    def test_pre_existing_legacy_is_left_alone(self, boot_dir: Path):
        """Normal ALREADY_NEW startup: pre-populated legacy preserved byte-for-byte."""
        _setup_new_grub_managed_boot(boot_dir)
        active_legacy = boot_dir / "ota-partition.sda3"
        active_legacy.mkdir(parents=True)
        (active_legacy / GRUB_KERNEL_FNAME).write_text("stable-kernel-bytes")
        (active_legacy / GRUB_INITRD_FNAME).write_text("stable-initrd-bytes")
        (active_legacy / "leftover").write_text("from-previous-boot")

        GrubBootController()

        assert (active_legacy / GRUB_KERNEL_FNAME).read_text() == "stable-kernel-bytes"
        assert (active_legacy / GRUB_INITRD_FNAME).read_text() == "stable-initrd-bytes"
        assert (active_legacy / "leftover").read_text() == "from-previous-boot"
        # And NOT re-hardlinked to the new slot folder.
        slot_a = boot_dir / "ota-slot_a"
        assert (active_legacy / GRUB_KERNEL_FNAME).stat().st_ino != (
            slot_a / GRUB_KERNEL_FNAME
        ).stat().st_ino

    def test_migrate_from_old_skips_ensure_step(self, boot_dir: Path):
        """MIGRATE_FROM_OLD: ensure-step skips; old-grub layout preserved."""
        _setup_old_grub_managed_boot(
            boot_dir, status="SUCCESS", slot_in_use="sda3", version="1.0.0"
        )
        active_legacy = boot_dir / "ota-partition.sda3"
        kernel_ino_before = (active_legacy / GRUB_KERNEL_FNAME).stat().st_ino
        assert (active_legacy / "vmlinuz-ota").is_symlink()

        GrubBootController()

        assert (active_legacy / "vmlinuz-ota").is_symlink()
        assert (active_legacy / "status").read_text() == "SUCCESS"
        assert (active_legacy / "slot_in_use").read_text() == "sda3"
        assert (active_legacy / GRUB_KERNEL_FNAME).stat().st_ino == kernel_ino_before


# ------------ Group A bootstrap inputs ------------ #


class TestGroupABootstrapInputs:
    """Real-file ``/boot/<vmlinuz-X>`` / ``<initrd.img-X>`` for Group A's bootstrap path (goal 4)."""

    @pytest.fixture(autouse=True)
    def setup_env(
        self, boot_dir, rootfs_dir, mnt_dir, mock_grub_commands, redirect_paths
    ):
        """Setup fixtures for the tests."""

    def test_fresh_keeps_kernel_and_initrd_at_boot_root(self, boot_dir: Path):
        _setup_fresh_grub_ecu_boot(boot_dir)
        GrubBootController()

        for fname in (GRUB_KERNEL_FNAME, GRUB_INITRD_FNAME):
            f = boot_dir / fname
            assert f.is_file() and not f.is_symlink()

    def test_post_update_keeps_active_and_standby_kernels_at_boot_root(
        self, boot_dir: Path, mnt_dir: Path, rootfs_dir: Path, mocker: MockerFixture
    ):
        """After OTA, ``/boot/`` has real-file kernels for both versions (Group A precondition)."""
        _setup_new_grub_managed_boot(boot_dir)
        slot_b_dir = boot_dir / "ota-slot_b"
        slot_b_dir.mkdir(parents=True, exist_ok=True)
        (slot_b_dir / "grub").mkdir(exist_ok=True)
        (boot_dir / "ota-slot_a" / "status").write_text("FAILURE")
        (boot_dir / "ota-slot_a" / "slot_in_use").write_text("ota-slot_b")

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
        # Bypass per-slot boot cfg generation for the cross-version case
        # (same pattern as test_post_update_cleans_old_boot_files).
        controller._boot_control.setup_ota_boot_cfg_for_slot = mocker.MagicMock()

        controller.post_update(update_version="3.0.0")

        assert (boot_dir / GRUB_KERNEL_FNAME).is_file()
        assert (boot_dir / GRUB_INITRD_FNAME).is_file()
        assert (boot_dir / GRUB_NEW_KERNEL_FNAME).is_file()
        assert (boot_dir / GRUB_NEW_INITRD_FNAME).is_file()


# ------------ Migrate + OTA full cycle ------------ #


class TestMigrateThenOTAFullCycle:
    """Plan §6.2: old-grub-managed → MIGRATE_FROM_OLD → new-grub OTA."""

    @pytest.fixture(autouse=True)
    def setup_env(
        self, boot_dir, rootfs_dir, mnt_dir, mock_grub_commands, redirect_paths
    ):
        """Setup fixtures for the tests."""

    def test_migrate_then_post_update(
        self, boot_dir: Path, mnt_dir: Path, rootfs_dir: Path, mocker: MockerFixture
    ):
        # Stage 1: old-grub-managed, slot_a active, SUCCESS.
        _setup_old_grub_managed_boot(
            boot_dir, status="SUCCESS", slot_in_use="sda3", version="1.0.0"
        )
        active_legacy = boot_dir / "ota-partition.sda3"
        standby_legacy = boot_dir / "ota-partition.sda4"
        active_legacy_files_before = {
            f.name for f in active_legacy.iterdir() if not f.name.startswith(".")
        }

        # Stage 2: new controller via MIGRATE_FROM_OLD.
        controller = GrubBootController()
        assert controller._boot_control.resetup_requested is False
        assert controller.get_booted_ota_status() == OTAStatus.SUCCESS
        slot_a_dir = boot_dir / "ota-slot_a"
        assert (slot_a_dir / "status").read_text() == "SUCCESS"
        assert (slot_a_dir / "slot_in_use").read_text() == "ota-slot_a"
        assert (slot_a_dir / "version").read_text() == "1.0.0"
        assert active_legacy_files_before == {
            f.name for f in active_legacy.iterdir() if not f.name.startswith(".")
        }

        # Stage 3: mimic pre_update side-effects.
        slot_b_dir = boot_dir / "ota-slot_b"
        slot_b_dir.mkdir(parents=True, exist_ok=True)
        (slot_b_dir / "grub").mkdir(exist_ok=True)
        (slot_a_dir / "status").write_text("FAILURE")
        (slot_a_dir / "slot_in_use").write_text("ota-slot_b")

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

        # Stage 4: run OTA.
        controller.post_update(update_version="3.0.0")

        assert (slot_b_dir / GRUB_KERNEL_FNAME).is_file()
        assert (slot_b_dir / GRUB_INITRD_FNAME).is_file()
        assert (slot_b_dir / "status").read_text() == "UPDATING"
        assert (slot_b_dir / "version").read_text() == "3.0.0"
        assert (slot_b_dir / "slot_in_use").read_text() == "ota-slot_b"
        assert not standby_legacy.exists()
        # Active legacy file-NAMES preserved (Group B predicate keeps relying on them).
        assert active_legacy_files_before == {
            f.name for f in active_legacy.iterdir() if not f.name.startswith(".")
        }
        assert (boot_dir / GRUB_KERNEL_FNAME).is_file()
        assert (boot_dir / GRUB_INITRD_FNAME).is_file()


# ------------ Per-helper tests ------------ #


class TestBootstrapSetupRootfsForOtaBoot:
    """Bootstrap-time fstab rewrite must preserve live mount options for / /boot /boot/efi.

    Regression: prior implementation dropped user options like ``noatime`` /
    ``umask=0077`` via ``_generate_fstab``'s hardcoded fallback.
    """

    _ACTIVE_FSUUID = "active-slot-uuid"
    _BOOT_UUID = "boot-part-uuid"
    _EFI_UUID = "efi-part-uuid"

    @staticmethod
    def _make_slot_mp(tmp_path: Path, fstab_text: str) -> Path:
        slot_mp = tmp_path / "slot_mp"
        (slot_mp / "etc" / "default").mkdir(parents=True)
        (slot_mp / "etc" / "grub.d").mkdir(parents=True)
        (slot_mp / "etc" / "fstab").write_text(fstab_text)
        (slot_mp / "etc" / "default" / "grub").write_text("# placeholder\n")
        return slot_mp

    @classmethod
    def _make_ctrl(cls, mocker) -> _GrubBootControl:
        ctrl = _make_grub_ctrl(mocker, boot_uuid=cls._BOOT_UUID, efi_uuid=cls._EFI_UUID)
        ctrl.boot_slots.current_slot = OTASlotBootID.slot_a
        ctrl.boot_slots.slot_a.uuid = cls._ACTIVE_FSUUID
        return ctrl

    def test_preserves_custom_mount_options_for_special_mounts(
        self, tmp_path: Path, mocker
    ):
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
        assert rewritten_lines[0] == custom_root
        assert rewritten_lines[1] == custom_boot
        assert rewritten_lines[2] == custom_efi

    def test_does_not_emit_fallback_warning(
        self, tmp_path: Path, mocker, caplog: pytest.LogCaptureFixture
    ):
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
        )

    def test_extra_mounts_carried_over(self, tmp_path: Path, mocker):
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


# Kernel/initrd are placed in the slot dir AND hardlinked at /boot root so old
# grub's bootstrap predicate (``is_file(/boot/<vmlinuz-uname-r>)``) passes.


class TestBootstrapSetupBootSlotDir:
    _KERNEL_VER = "6.11.0-29-generic"

    @staticmethod
    def _make_src_files(src_dir: Path, kernel_ver: str) -> tuple[Path, Path]:
        src_dir.mkdir(parents=True, exist_ok=True)
        kernel = src_dir / f"{VMLINUZ_PREFIX}{kernel_ver}"
        initrd = src_dir / f"{INITRD_PREFIX}{kernel_ver}"
        kernel.write_bytes(b"kernel-content")
        initrd.write_bytes(b"initrd-content")
        return kernel, initrd

    def test_places_files_in_slot_dir_and_hardlinks_at_boot_root(
        self, tmp_path, mocker
    ):
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
        assert (slot_boot_dir / src_kernel.name).read_bytes() == b"kernel-content"
        assert (slot_boot_dir / src_initrd.name).read_bytes() == b"initrd-content"
        for fname in (src_kernel.name, src_initrd.name):
            slot_path = slot_boot_dir / fname
            root_path = tmp_path / fname
            assert root_path.is_file() and not root_path.is_symlink()
            assert slot_path.stat().st_ino == root_path.stat().st_ino

    def test_replaces_stale_boot_root_file(self, tmp_path, mocker):
        """Pre-existing ``/boot/<vmlinuz>`` is replaced with a fresh hardlink."""
        slot_id = OTASlotBootID.slot_a
        _patch_boot_dpath(mocker, tmp_path)
        ctrl = _make_partial_grub_ctrl(mocker, current_slot=slot_id)

        src_kernel, src_initrd = self._make_src_files(
            tmp_path / "ota-partition.sda3", self._KERNEL_VER
        )
        (tmp_path / src_kernel.name).write_bytes(b"stale-content")

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
        """Source already in slot dir → hardlinked at /boot root, no re-copy."""
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

        assert src_kernel.stat().st_ino == src_kernel_ino
        assert src_initrd.stat().st_ino == src_initrd_ino
        assert (tmp_path / src_kernel.name).stat().st_ino == src_kernel_ino
        assert (tmp_path / src_initrd.name).stat().st_ino == src_initrd_ino


class TestSetupBootSlotDir:
    """``setup_boot_slot_dir`` (post-OTA): copy kernel/initrd into slot dir + hardlink at /boot root."""

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

    def test_places_files_in_slot_dir_and_hardlinks_at_boot_root(
        self, tmp_path, mocker
    ):
        slot_id = OTASlotBootID.slot_b
        boot_dir, slot_boot_dir, slot_mp = self._setup_dirs(tmp_path, slot_id)
        _patch_boot_dpath(mocker, boot_dir)
        ctrl = _make_partial_grub_ctrl(mocker)
        kernel, initrd = self._make_slot_files(slot_mp, self._KERNEL_VER)

        ctrl.setup_boot_slot_dir(self._KERNEL_VER, slot_id=slot_id, slot_mp=slot_mp)

        assert (slot_boot_dir / kernel.name).read_bytes() == b"new-kernel"
        assert (slot_boot_dir / initrd.name).read_bytes() == b"new-initrd"
        for fname in (kernel.name, initrd.name):
            slot_path = slot_boot_dir / fname
            root_path = boot_dir / fname
            assert root_path.is_file() and not root_path.is_symlink()
            assert slot_path.stat().st_ino == root_path.stat().st_ino

    def test_replaces_stale_boot_root_file(self, tmp_path, mocker):
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
        """Symlinked ``vmlinuz-<ver>`` in the slot mount point → skipped (no copy, no hardlink)."""
        slot_id = OTASlotBootID.slot_b
        boot_dir, slot_boot_dir, slot_mp = self._setup_dirs(tmp_path, slot_id)
        _patch_boot_dpath(mocker, boot_dir)
        ctrl = _make_partial_grub_ctrl(mocker)

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


# ------------ Setup-case detection ------------ #


class TestDetectBootControlSetupCase:
    """``_detect_boot_control_setup_case`` classifies the three grub.cfg layouts."""

    @pytest.fixture
    def grub_cfg_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        target = tmp_path / "grub.cfg"
        monkeypatch.setattr(
            "otaclient.boot_control.configs.GrubControlNewConfig.GRUB_CFG_FPATH",
            str(target),
        )
        return target

    def test_fresh_when_grub_cfg_missing(self, grub_cfg_path: Path):
        assert not grub_cfg_path.exists()
        assert _detect_boot_control_setup_case() == _GrubBootControlSetupCase.FRESH

    def test_fresh_when_grub_cfg_unmanaged(self, grub_cfg_path: Path):
        grub_cfg_path.write_text(GRUB_CFG_CONTENT)
        assert _detect_boot_control_setup_case() == _GrubBootControlSetupCase.FRESH

    def test_fresh_when_grub_cfg_unreadable(self, grub_cfg_path: Path):
        grub_cfg_path.write_text(GRUB_CFG_CONTENT)
        from unittest.mock import patch

        with patch.object(Path, "read_text", side_effect=OSError("denied")):
            assert _detect_boot_control_setup_case() == _GrubBootControlSetupCase.FRESH

    def test_migrate_when_grub_cfg_is_symlink(self, grub_cfg_path: Path):
        """Symlink → MIGRATE_FROM_OLD (only old grub maintains this layout)."""
        grub_cfg_path.symlink_to("../ota-partition/grub.cfg")
        assert (
            _detect_boot_control_setup_case()
            == _GrubBootControlSetupCase.MIGRATE_FROM_OLD
        )

    def test_already_new_when_managed_footer_valid(self, grub_cfg_path: Path):
        managed = OTAManagedCfg(
            raw_contents=GRUB_CFG_CONTENT.strip(), grub_version="2.12"
        )
        grub_cfg_path.write_text(managed.export())
        assert (
            _detect_boot_control_setup_case() == _GrubBootControlSetupCase.ALREADY_NEW
        )
