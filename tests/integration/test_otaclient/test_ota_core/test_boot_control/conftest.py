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
"""Shared fixtures for grub boot controller integration tests."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from otaclient.boot_control._grub_common import (
    ABPartition,
    OTAManagedCfg,
    OTASlotBootID,
    SlotInfo,
)
from otaclient.configs._cfg_consts import cfg_consts
from otaclient_common import _io as _io_module

#
# ------------ Test constants ------------ #
#
GRUB_KERNEL_VERSION = "6.11.0-29-generic"
GRUB_SLOT_A_UUID = "186d206e-73e7-401c-9d8a-fad4390922f2"
GRUB_SLOT_B_UUID = "286d206e-73e7-401c-9d8a-fad4390922f3"
GRUB_BOOT_PART_UUID = "30cca32a-cbf5-4b05-8934-c6887a134162"
GRUB_EFI_PART_UUID = "AAAA-BBBB"

GRUB_KERNEL_FNAME = f"vmlinuz-{GRUB_KERNEL_VERSION}"
GRUB_INITRD_FNAME = f"initrd.img-{GRUB_KERNEL_VERSION}"

# Cross-version constants used by post-update tests where the standby image
# carries a different kernel than the active.
GRUB_NEW_KERNEL_VERSION = "6.12.0-1-generic"
GRUB_NEW_KERNEL_FNAME = f"vmlinuz-{GRUB_NEW_KERNEL_VERSION}"
GRUB_NEW_INITRD_FNAME = f"initrd.img-{GRUB_NEW_KERNEL_VERSION}"

TEST_DATA_DIR = Path(__file__).parents[4] / "data"
# The test data grub.cfg uses 5.19.0-50-generic; replace with our test version.
GRUB_CFG_CONTENT = (
    (TEST_DATA_DIR / "grub.cfg")
    .read_text()
    .replace("5.19.0-50-generic", GRUB_KERNEL_VERSION)
)
GRUB_DEFAULT_CONTENT = (TEST_DATA_DIR / "grub_default").read_text()

GRUB_SAMPLE_FSTAB = (
    f"UUID={GRUB_SLOT_A_UUID}\t/\text4\terrors=remount-ro\t0\t1\n"
    f"UUID={GRUB_BOOT_PART_UUID}\t/boot\text4\tdefaults\t0\t1\n"
    f"UUID={GRUB_EFI_PART_UUID}\t/boot/efi\tvfat\tdefaults\t0\t1\n"
)


#
# ------------ ABPartition fixture ------------ #
#
# UEFI layout, current=slot_a.
@pytest.fixture
def ab_partition() -> ABPartition:
    return ABPartition(
        is_uefi=True,
        efi_partition=SlotInfo(dev="/dev/sda1", uuid=GRUB_EFI_PART_UUID),
        boot_partition=SlotInfo(dev="/dev/sda2", uuid=GRUB_BOOT_PART_UUID),
        slot_a=SlotInfo(
            dev="/dev/sda3", uuid=GRUB_SLOT_A_UUID, slot_id=OTASlotBootID.slot_a
        ),
        slot_b=SlotInfo(
            dev="/dev/sda4", uuid=GRUB_SLOT_B_UUID, slot_id=OTASlotBootID.slot_b
        ),
        current_slot=OTASlotBootID.slot_a,
        standby_slot=OTASlotBootID.slot_b,
        old_slot_id_mapping={
            OTASlotBootID.slot_a: "ota-partition.sda3",
            OTASlotBootID.slot_b: "ota-partition.sda4",
        },
    )


#
# ------------ Filesystem layout helpers ------------ #
#
def _create_dummy_grub_kernel(directory: Path) -> None:
    """Create dummy kernel and initrd files in the given directory."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / GRUB_KERNEL_FNAME).write_text("dummy-kernel")
    (directory / GRUB_INITRD_FNAME).write_text("dummy-initrd")


def _create_grub_rootfs(rootfs_dir: Path) -> None:
    """Create minimal rootfs structure: /etc/fstab, /etc/default/grub, /etc/grub.d/."""
    (rootfs_dir / "etc" / "default").mkdir(parents=True)
    (rootfs_dir / "etc" / "grub.d").mkdir(parents=True)
    (rootfs_dir / "etc" / "fstab").write_text(GRUB_SAMPLE_FSTAB)
    (rootfs_dir / "etc" / "default" / "grub").write_text(GRUB_DEFAULT_CONTENT)


#
# ------------ /boot filesystem scenario builders ------------ #
#
# Shared between the boot-controller integration tests.
def _setup_fresh_grub_ecu_boot(boot_dir: Path) -> None:
    """Set up /boot as a fresh ECU, not yet managed by OTA.

    Kernel and initrd are at /boot root; ``grub.cfg`` is a plain
    (non-OTA-managed) regular file.
    """
    _create_dummy_grub_kernel(boot_dir)
    (boot_dir / "grub" / "grub.cfg").write_text(GRUB_CFG_CONTENT)


def _setup_old_grub_managed_boot(
    boot_dir: Path,
    *,
    status: str = "SUCCESS",
    slot_in_use: str = "sda3",
    version: str = "1.0.0",
) -> None:
    """Set up /boot as if managed by the old grub boot control.

    Old layout::

        /boot/grub/grub.cfg            -> ../ota-partition/grub.cfg  (symlink)
        /boot/ota-partition            -> ota-partition.sda3         (symlink)
        /boot/ota-partition.sda3/                                    (active)
        /boot/ota-partition.sda4/                                    (standby)
        /boot/vmlinuz-ota              -> ota-partition/vmlinuz-ota  (symlink)
        /boot/initrd.img-ota           -> ota-partition/initrd.img-ota
        /boot/vmlinuz-ota.standby      -> ota-partition.sda4/vmlinuz-ota
        /boot/initrd.img-ota.standby   -> ota-partition.sda4/initrd.img-ota

    No actual kernel files at /boot root. The active slot's status /
    slot_in_use / version files are parametrised so the migration tests can
    drive the various carryover cases.
    """
    active_part = boot_dir / "ota-partition.sda3"
    active_part.mkdir(parents=True)
    (active_part / GRUB_KERNEL_FNAME).write_text("dummy-kernel")
    (active_part / GRUB_INITRD_FNAME).write_text("dummy-initrd")
    (active_part / "vmlinuz-ota").symlink_to(GRUB_KERNEL_FNAME)
    (active_part / "initrd.img-ota").symlink_to(GRUB_INITRD_FNAME)
    (active_part / "grub.cfg").write_text(GRUB_CFG_CONTENT)
    (active_part / "status").write_text(status)
    (active_part / "slot_in_use").write_text(slot_in_use)
    (active_part / "version").write_text(version)

    standby_part = boot_dir / "ota-partition.sda4"
    standby_part.mkdir(parents=True)
    (standby_part / GRUB_KERNEL_FNAME).write_text("dummy-kernel-standby")
    (standby_part / GRUB_INITRD_FNAME).write_text("dummy-initrd-standby")
    (standby_part / "vmlinuz-ota").symlink_to(GRUB_KERNEL_FNAME)
    (standby_part / "initrd.img-ota").symlink_to(GRUB_INITRD_FNAME)

    (boot_dir / "ota-partition").symlink_to("ota-partition.sda3")

    grub_cfg_path = boot_dir / "grub" / "grub.cfg"
    grub_cfg_path.unlink(missing_ok=True)
    grub_cfg_path.symlink_to("../ota-partition/grub.cfg")

    (boot_dir / "vmlinuz-ota").symlink_to("ota-partition/vmlinuz-ota")
    (boot_dir / "initrd.img-ota").symlink_to("ota-partition/initrd.img-ota")
    (boot_dir / "vmlinuz-ota.standby").symlink_to("ota-partition.sda4/vmlinuz-ota")
    (boot_dir / "initrd.img-ota.standby").symlink_to(
        "ota-partition.sda4/initrd.img-ota"
    )


def _setup_new_grub_managed_boot(
    boot_dir: Path,
    *,
    ota_status: str = "SUCCESS",
    slot_in_use: str = "ota-slot_a",
) -> None:
    """Set up /boot as if already managed by the new grub boot control."""
    slot_a_dir = boot_dir / "ota-slot_a"
    _create_dummy_grub_kernel(slot_a_dir)
    (slot_a_dir / "grub").mkdir(exist_ok=True)
    (slot_a_dir / "status").write_text(ota_status)
    (slot_a_dir / "slot_in_use").write_text(slot_in_use)
    (slot_a_dir / "version").write_text("2.0.0")

    slot_b_dir = boot_dir / "ota-slot_b"
    slot_b_dir.mkdir(parents=True, exist_ok=True)
    (slot_b_dir / "grub").mkdir(exist_ok=True)

    managed_cfg = OTAManagedCfg(
        raw_contents=GRUB_CFG_CONTENT.strip(),
        grub_version="2.12",
    )
    (boot_dir / "grub" / "grub.cfg").write_text(managed_cfg.export())
    (boot_dir / "grub" / "grubenv").write_text("saved_entry=ota-slot_a\n")
    (boot_dir / "grub" / "ota-slot_a.cfg").write_text(
        f"menuentry 'ota-slot_a' {{ linux /ota-slot_a/vmlinuz-{GRUB_KERNEL_VERSION} }}\n"
    )

    (boot_dir / "efi").mkdir(exist_ok=True)
    (boot_dir / GRUB_KERNEL_FNAME).write_text("dummy-kernel")
    (boot_dir / GRUB_INITRD_FNAME).write_text("dummy-initrd")


#
# ------------ /boot filesystem setup fixtures ------------ #
#
@pytest.fixture
def boot_dir(tmp_path: Path) -> Path:
    """Return the base /boot directory under tmp_path, pre-created."""
    boot = tmp_path / "boot"
    (boot / "grub").mkdir(parents=True)
    return boot


@pytest.fixture
def rootfs_dir(tmp_path: Path) -> Path:
    """Return the rootfs directory under tmp_path, pre-created with minimal content."""
    rootfs = tmp_path / "rootfs"
    _create_grub_rootfs(rootfs)
    return rootfs


@pytest.fixture
def mnt_dir(tmp_path: Path) -> Path:
    """Return the mount space directory under tmp_path."""
    mnt = tmp_path / "mnt"
    (mnt / "standby").mkdir(parents=True)
    (mnt / "active").mkdir(parents=True)
    return mnt


#
# ------------ Mock commands fixture ------------ #
#
@pytest.fixture
def mock_grub_commands(
    monkeypatch, boot_dir: Path, rootfs_dir: Path, ab_partition: ABPartition
):
    """Patch all external commands and system calls needed by the grub controller.

    Returns a dict of mock objects for assertions.
    """
    mocks = {}

    # --- ABPartitionDetector.detect_boot_slots ---
    monkeypatch.setattr(
        "otaclient.boot_control._grub_new.ABPartitionDetector.detect_boot_slots",
        lambda: ab_partition,
    )

    # --- cmdhelper calls ---
    monkeypatch.setattr(
        "otaclient.boot_control._grub_new.cmdhelper.get_kernel_version_via_uname",
        lambda: GRUB_KERNEL_VERSION,
    )
    monkeypatch.setattr(
        "otaclient.boot_control._grub_new.cmdhelper.bind_mount_rw",
        lambda src, dst: shutil.copytree(src, dst, dirs_exist_ok=True),
    )
    monkeypatch.setattr(
        "otaclient.boot_control._grub_new.cmdhelper.ensure_umount",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "otaclient.boot_control._grub_new.cmdhelper.mount",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "otaclient.boot_control._grub_new.cmdhelper.umount",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "otaclient.boot_control._grub_new.cmdhelper.reboot",
        lambda *a, **kw: None,
    )

    # --- _env calls ---
    # Return rootfs_dir so that bootstrap bind_mount_rw copies our test rootfs
    # (instead of "/" which would copy the entire real filesystem)
    monkeypatch.setattr(
        "otaclient.boot_control._grub_new._env.get_dynamic_client_chroot_path",
        lambda: str(rootfs_dir),
    )
    monkeypatch.setattr(
        "otaclient.boot_control._ota_status_control._env.is_running_as_downloaded_dynamic_app",
        lambda: False,
    )
    monkeypatch.setattr(
        "otaclient.boot_control._slot_mnt_helper._env.is_dynamic_client_running",
        lambda: False,
    )

    # --- SlotMountHelper: suppress atexit registration ---
    monkeypatch.setattr(
        "otaclient.boot_control._slot_mnt_helper.atexit.register",
        lambda *a, **kw: None,
    )

    # --- remove_file: intercept hardcoded "/boot/ota-partition" ---
    # Line 702 of _grub_new.py has `remove_file("/boot/ota-partition")` which is
    # a hardcoded absolute path. We wrap the real remove_file to redirect any
    # absolute path starting with "/boot" to our test boot_dir.
    _real_remove_file = _io_module.remove_file

    def _safe_remove_file(_fpath, *, ignore_error=True):
        _fpath_str = str(_fpath)
        if _fpath_str.startswith("/boot"):
            _fpath = Path(str(boot_dir) + _fpath_str[len("/boot") :])
        _real_remove_file(_fpath, ignore_error=ignore_error)

    monkeypatch.setattr(
        "otaclient.boot_control._grub_new.remove_file", _safe_remove_file
    )

    # --- _detect_grub_version ---
    monkeypatch.setattr(
        "otaclient.boot_control._grub_new._GrubBootHelperFuncs._detect_grub_version",
        staticmethod(lambda _slot_mp: "2.12"),
    )

    # --- _grub_mkconfig_on_mp ---
    monkeypatch.setattr(
        "otaclient.boot_control._grub_new._GrubBootHelperFuncs._grub_mkconfig_on_mp",
        staticmethod(lambda _slot_mp, _boot_source: GRUB_CFG_CONTENT),
    )

    # --- _write_grubenv_atomic: write options as plain text to grubenv ---
    def _mock_write_grubenv_atomic(options: list[str]) -> None:
        grubenv = boot_dir / "grub" / "grubenv"
        grubenv.write_text("\n".join(options) + "\n")

    monkeypatch.setattr(
        "otaclient.boot_control._grub_new._GrubBootHelperFuncs._write_grubenv_atomic",
        staticmethod(_mock_write_grubenv_atomic),
    )

    return mocks


#
# ------------ Path redirection fixture ------------ #
#
@pytest.fixture
def redirect_paths(
    monkeypatch, tmp_path: Path, boot_dir: Path, rootfs_dir: Path, mnt_dir: Path
):
    """Redirect all boot_cfg and cfg path constants to tmp_path subdirectories."""
    boot_cfg_module = "otaclient.boot_control.configs.GrubControlNewConfig"

    # boot_cfg paths
    monkeypatch.setattr(f"{boot_cfg_module}.BOOT_DPATH", str(boot_dir))
    monkeypatch.setattr(f"{boot_cfg_module}.GRUB_DIR", str(boot_dir / "grub"))
    monkeypatch.setattr(
        f"{boot_cfg_module}.GRUB_CFG_FPATH", str(boot_dir / "grub" / "grub.cfg")
    )
    monkeypatch.setattr(
        f"{boot_cfg_module}.GRUBENV_FPATH", str(boot_dir / "grub" / "grubenv")
    )
    monkeypatch.setattr(
        f"{boot_cfg_module}.DEFAULT_GRUB_PATH",
        str(rootfs_dir / "etc" / "default" / "grub"),
    )
    monkeypatch.setattr(
        f"{boot_cfg_module}.FSTAB_FILE_PATH", str(rootfs_dir / "etc" / "fstab")
    )
    monkeypatch.setattr(
        f"{boot_cfg_module}.GRUB_HOOKS_DPATH", str(rootfs_dir / "etc" / "grub.d")
    )

    # cfg (Consts) paths — patch the underlying _cfg_consts attributes
    consts_module = "otaclient.configs._cfg_consts.Consts"
    monkeypatch.setattr(f"{consts_module}.MOUNT_SPACE", str(mnt_dir))
    monkeypatch.setattr(f"{consts_module}.STANDBY_SLOT_MNT", str(mnt_dir / "standby"))
    monkeypatch.setattr(f"{consts_module}.ACTIVE_SLOT_MNT", str(mnt_dir / "active"))
    monkeypatch.setattr(f"{consts_module}.BOOT_DPATH", str(boot_dir))
    monkeypatch.setattr(f"{consts_module}.EFI_DPATH", str(boot_dir / "efi"))
    monkeypatch.setattr(f"{consts_module}.CANONICAL_ROOT", str(rootfs_dir))

    # OTA config paths — must be under CANONICAL_ROOT for replace_root to work
    monkeypatch.setattr(f"{consts_module}.OTA_DPATH", str(rootfs_dir / "boot" / "ota"))
    monkeypatch.setattr(
        f"{consts_module}.PROXY_INFO_FPATH",
        str(rootfs_dir / "boot" / "ota" / "proxy_info.yaml"),
    )
    monkeypatch.setattr(
        f"{consts_module}.ECU_INFO_FPATH",
        str(rootfs_dir / "boot" / "ota" / "ecu_info.yaml"),
    )

    # ACTIVE_ROOT is a property reading _ACTIVE_ROOT, so patch _ACTIVE_ROOT on the instance
    monkeypatch.setattr(cfg_consts, "_ACTIVE_ROOT", str(rootfs_dir))
