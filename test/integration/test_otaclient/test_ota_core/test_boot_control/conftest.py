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
    OTASlotBootID,
    SlotInfo,
)
from otaclient.configs._cfg_consts import cfg_consts
from otaclient_common import _io as _io_module

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------
KERNEL_VERSION = "6.11.0-29-generic"
SLOT_A_UUID = "186d206e-73e7-401c-9d8a-fad4390922f2"
SLOT_B_UUID = "286d206e-73e7-401c-9d8a-fad4390922f3"
BOOT_PART_UUID = "30cca32a-cbf5-4b05-8934-c6887a134162"
EFI_PART_UUID = "AAAA-BBBB"

KERNEL_FNAME = f"vmlinuz-{KERNEL_VERSION}"
INITRD_FNAME = f"initrd.img-{KERNEL_VERSION}"

TEST_DATA_DIR = Path(__file__).parents[4] / "data"
# The test data grub.cfg uses 5.19.0-50-generic; replace with our test version.
GRUB_CFG_CONTENT = (
    (TEST_DATA_DIR / "grub.cfg")
    .read_text()
    .replace("5.19.0-50-generic", KERNEL_VERSION)
)
GRUB_DEFAULT_CONTENT = (TEST_DATA_DIR / "grub_default").read_text()

SAMPLE_FSTAB = (
    f"UUID={SLOT_A_UUID}\t/\text4\terrors=remount-ro\t0\t1\n"
    f"UUID={BOOT_PART_UUID}\t/boot\text4\tdefaults\t0\t1\n"
    f"UUID={EFI_PART_UUID}\t/boot/efi\tvfat\tdefaults\t0\t1\n"
)


# ---------------------------------------------------------------------------
# ABPartition fixture — UEFI layout, current=slot_a
# ---------------------------------------------------------------------------
@pytest.fixture
def ab_partition() -> ABPartition:
    return ABPartition(
        is_uefi=True,
        efi_partition=SlotInfo(dev="/dev/sda1", uuid=EFI_PART_UUID),
        boot_partition=SlotInfo(dev="/dev/sda2", uuid=BOOT_PART_UUID),
        slot_a=SlotInfo(
            dev="/dev/sda3", uuid=SLOT_A_UUID, slot_id=OTASlotBootID.slot_a
        ),
        slot_b=SlotInfo(
            dev="/dev/sda4", uuid=SLOT_B_UUID, slot_id=OTASlotBootID.slot_b
        ),
        current_slot=OTASlotBootID.slot_a,
        standby_slot=OTASlotBootID.slot_b,
        old_slot_id_mapping={
            OTASlotBootID.slot_a: "ota-partition.sda3",
            OTASlotBootID.slot_b: "ota-partition.sda4",
        },
    )


# ---------------------------------------------------------------------------
# Filesystem layout helpers
# ---------------------------------------------------------------------------
def _create_dummy_kernel(directory: Path) -> None:
    """Create dummy kernel and initrd files in the given directory."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / KERNEL_FNAME).write_text("dummy-kernel")
    (directory / INITRD_FNAME).write_text("dummy-initrd")


def _create_rootfs(rootfs_dir: Path) -> None:
    """Create minimal rootfs structure: /etc/fstab, /etc/default/grub, /etc/grub.d/."""
    (rootfs_dir / "etc" / "default").mkdir(parents=True)
    (rootfs_dir / "etc" / "grub.d").mkdir(parents=True)
    (rootfs_dir / "etc" / "fstab").write_text(SAMPLE_FSTAB)
    (rootfs_dir / "etc" / "default" / "grub").write_text(GRUB_DEFAULT_CONTENT)


# ---------------------------------------------------------------------------
# /boot filesystem setup fixtures
# ---------------------------------------------------------------------------
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
    _create_rootfs(rootfs)
    return rootfs


@pytest.fixture
def mnt_dir(tmp_path: Path) -> Path:
    """Return the mount space directory under tmp_path."""
    mnt = tmp_path / "mnt"
    (mnt / "standby").mkdir(parents=True)
    (mnt / "active").mkdir(parents=True)
    return mnt


# ---------------------------------------------------------------------------
# Mock commands fixture
# ---------------------------------------------------------------------------
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
        lambda: KERNEL_VERSION,
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


# ---------------------------------------------------------------------------
# Path redirection fixture
# ---------------------------------------------------------------------------
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
