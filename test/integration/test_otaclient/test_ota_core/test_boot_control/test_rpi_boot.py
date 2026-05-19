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
"""Integration tests for the RPI boot controller.

These tests verify:
  - Startup flow: RPIBootController.__init__ correctly probes the AB layout,
    validates boot files, and (when applicable) finalizes a pending switch boot.
  - Pre-update hooks: RPIBootController._pre_update_prepare_standby passes the
    standby fslabel through to SlotMountHelper.prepare_standby_dev, and the
    template method updates current-slot OTA status.
  - Post-update hooks: RPIBootController._post_update_platform_specific copies
    /boot/ota, writes standby fstab, runs flash-kernel, and prepares
    tryboot.txt for the standby slot.
  - finalizing_update: triggers a tryboot reboot via cmdhelper.reboot.
"""

from __future__ import annotations

from pathlib import Path
from string import Template

import pytest
from pytest_mock import MockerFixture

from otaclient._types import OTAStatus
from otaclient.boot_control import _rpi_boot
from otaclient.boot_control._rpi_boot import (
    CMDLINE_TXT,
    CONFIG_TXT,
    INITRD_IMG,
    SEP_CHAR,
    TRYBOOT_TXT,
    VMLINUZ,
    RPIBootController,
)
from otaclient.errors import (
    BootControlPostUpdateFailed,
    BootControlPreUpdateFailed,
    BootControlStartupFailed,
)

#
# ------------ Test constants ------------ #
#
SLOT_A = "slot_a"
SLOT_B = "slot_b"

PARENT_DEV = "/dev/sda"
SYSTEM_BOOT_DEV = "/dev/sda1"
SLOT_A_DEV = "/dev/sda2"
SLOT_B_DEV = "/dev/sda3"
# NOTE: rpi_boot allows extra partitions after sd<x>3
DEVICE_TREE = [PARENT_DEV, SYSTEM_BOOT_DEV, SLOT_A_DEV, SLOT_B_DEV, "/dev/sda5"]

RPI_MODEL = "Raspberry Pi 4 Model B"

CONFIG_TXT_SLOT_A = "config_txt_slot_a"
CONFIG_TXT_SLOT_B = "config_txt_slot_b"
CMDLINE_TXT_SLOT_A = "cmdline_txt_slot_a"
CMDLINE_TXT_SLOT_B = "cmdline_txt_slot_b"

CURRENT_VERSION = "1.2.3"
UPDATE_VERSION = "rpi_boot_test"


#
# ------------ Filesystem layout helpers ------------ #
#
def _setup_system_boot_dir(system_boot_dir: Path) -> None:
    """Populate the system-boot vfat partition with per-slot boot files.

    Pre-update layout:
      - config.txt              (currently active slot's content)
      - config.txt_slot_a       (active boot cfg)
      - config.txt_slot_b       (standby boot cfg)
      - cmdline.txt_slot_{a,b}  (kernel cmdline per slot)
      - vmlinuz_slot_a          (active kernel)
      - initrd.img_slot_a       (active initrd)
    """
    system_boot_dir.mkdir(parents=True, exist_ok=True)
    (system_boot_dir / CONFIG_TXT).write_text(CONFIG_TXT_SLOT_A)
    (system_boot_dir / f"{CONFIG_TXT}{SEP_CHAR}{SLOT_A}").write_text(CONFIG_TXT_SLOT_A)
    (system_boot_dir / f"{CONFIG_TXT}{SEP_CHAR}{SLOT_B}").write_text(CONFIG_TXT_SLOT_B)
    (system_boot_dir / f"{CMDLINE_TXT}{SEP_CHAR}{SLOT_A}").write_text(
        CMDLINE_TXT_SLOT_A
    )
    (system_boot_dir / f"{CMDLINE_TXT}{SEP_CHAR}{SLOT_B}").write_text(
        CMDLINE_TXT_SLOT_B
    )
    (system_boot_dir / f"{VMLINUZ}{SEP_CHAR}{SLOT_A}").write_text("slot_a-vmlinuz")
    (system_boot_dir / f"{INITRD_IMG}{SEP_CHAR}{SLOT_A}").write_text("slot_a-initrd")


def _setup_slot_a_active(
    slot_a_mp: Path,
    *,
    ota_status: str = OTAStatus.SUCCESS,
    slot_in_use: str = SLOT_A,
    version: str = CURRENT_VERSION,
) -> Path:
    """Set up slot_a as the active slot with the given OTA status state.

    Returns the slot_a's ota-status directory path.
    """
    ota_status_dir = slot_a_mp / Path(_rpi_boot.boot_cfg.OTA_STATUS_DIR).relative_to(
        "/"
    )
    ota_status_dir.mkdir(parents=True, exist_ok=True)
    (ota_status_dir / "status").write_text(ota_status)
    (ota_status_dir / "slot_in_use").write_text(slot_in_use)
    (ota_status_dir / "version").write_text(version)

    # /boot/ota directory exists on active slot for preserve_ota_folder_to_standby
    (slot_a_mp / "boot" / "ota").mkdir(parents=True, exist_ok=True)

    # /boot/ kernel and initrd symlinks targeted by post-update flash-kernel mock
    slot_a_boot = slot_a_mp / "boot"
    slot_a_boot.mkdir(parents=True, exist_ok=True)
    (slot_a_boot / VMLINUZ).write_text("slot_a-kernel-real")
    (slot_a_boot / INITRD_IMG).write_text("slot_a-initrd-real")
    return ota_status_dir


def _setup_slot_b_post_switch(
    slot_b_mp: Path,
    *,
    ota_status: str = OTAStatus.UPDATING,
    slot_in_use: str = SLOT_B,
    version: str = UPDATE_VERSION,
) -> Path:
    """Set up slot_b's ota-status to simulate a successful pre/post update.

    Used by the first-reboot/finalize-switch-boot test where the controller
    re-starts on slot_b with status=UPDATING.
    """
    ota_status_dir = slot_b_mp / Path(_rpi_boot.boot_cfg.OTA_STATUS_DIR).relative_to(
        "/"
    )
    ota_status_dir.mkdir(parents=True, exist_ok=True)
    (ota_status_dir / "status").write_text(ota_status)
    (ota_status_dir / "slot_in_use").write_text(slot_in_use)
    (ota_status_dir / "version").write_text(version)

    # ensure /etc/ exists on slot_b so write_str_to_file_atomic for fstab works
    (slot_b_mp / "etc").mkdir(parents=True, exist_ok=True)

    # /boot/ota dir on slot_b for preserve_ota_folder_to_standby destination
    (slot_b_mp / "boot" / "ota").mkdir(parents=True, exist_ok=True)
    return ota_status_dir


#
# ------------ Filesystem fixtures ------------ #
#
@pytest.fixture
def slot_a_mp(tmp_path: Path) -> Path:
    p = tmp_path / "slot_a"
    p.mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture
def slot_b_mp(tmp_path: Path) -> Path:
    p = tmp_path / "slot_b"
    p.mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture
def system_boot_dir(tmp_path: Path) -> Path:
    p = tmp_path / "system-boot"
    p.mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture
def model_file(tmp_path: Path) -> Path:
    p = tmp_path / "model"
    p.write_text(RPI_MODEL)
    return p


#
# ------------ Path redirection ------------ #
#
# Patches all RPI boot_cfg + Consts paths to tmp_path.
def _apply_rpi_path_redirects(
    monkeypatch,
    *,
    system_boot_dir: Path,
    active_mp: Path,
    standby_mp: Path,
    model_file: Path,
) -> None:
    """Patch rpi_boot_cfg + Consts paths so the controller targets tmp_path."""
    boot_cfg_module = "otaclient.boot_control.configs.RPIBootControlConfig"
    monkeypatch.setattr(
        f"{boot_cfg_module}.SYSTEM_BOOT_MOUNT_POINT", str(system_boot_dir)
    )
    monkeypatch.setattr(f"{boot_cfg_module}.RPI_MODEL_FILE", str(model_file))

    consts_module = "otaclient.configs._cfg_consts.Consts"
    monkeypatch.setattr(f"{consts_module}.ACTIVE_SLOT_MNT", str(active_mp))
    monkeypatch.setattr(f"{consts_module}.STANDBY_SLOT_MNT", str(standby_mp))
    # ACTIVE_ROOT is a property; replace it at the class level so every
    # Consts instance (including the one in otaclient.configs.cfg) observes
    # the redirected root.
    monkeypatch.setattr(f"{consts_module}.ACTIVE_ROOT", str(active_mp))


@pytest.fixture
def redirect_rpi_paths(
    monkeypatch,
    system_boot_dir: Path,
    slot_a_mp: Path,
    slot_b_mp: Path,
    model_file: Path,
):
    """Path redirects with slot_a as the active slot (the default scenario)."""
    _apply_rpi_path_redirects(
        monkeypatch,
        system_boot_dir=system_boot_dir,
        active_mp=slot_a_mp,
        standby_mp=slot_b_mp,
        model_file=model_file,
    )


@pytest.fixture
def redirect_rpi_paths_slot_b_active(
    monkeypatch,
    system_boot_dir: Path,
    slot_a_mp: Path,
    slot_b_mp: Path,
    model_file: Path,
):
    """Path redirects with slot_b as the active slot.

    Used by the finalize-switch-boot test that simulates the first reboot
    after a slot_a → slot_b update.
    """
    _apply_rpi_path_redirects(
        monkeypatch,
        system_boot_dir=system_boot_dir,
        active_mp=slot_b_mp,
        standby_mp=slot_a_mp,
        model_file=model_file,
    )


#
# ------------ Command / environment mocks ------------ #
#
def _install_cmdhelper_mocks(
    monkeypatch,
    *,
    active_slot_dev: str,
    active_fslabel: str | None,
    system_boot_mounted: bool = True,
):
    """Patch cmdhelper symbols imported by _rpi_boot to in-memory stand-ins."""

    def _get_current_rootfs_dev(*_, **__):
        return active_slot_dev

    def _get_parent_dev(_dev, *_, **__):
        return PARENT_DEV

    def _get_device_tree(parent_dev, *_, **__):
        assert parent_dev == PARENT_DEV
        return list(DEVICE_TREE)

    def _get_attrs_by_dev(token, dev, *_, **__):
        # rpi_boot only queries LABEL on the active slot dev for fslabel checks
        if token == "LABEL" and dev == active_slot_dev:
            return active_fslabel
        return ""

    monkeypatch.setattr(
        "otaclient.boot_control._rpi_boot.cmdhelper.get_current_rootfs_dev",
        _get_current_rootfs_dev,
    )
    monkeypatch.setattr(
        "otaclient.boot_control._rpi_boot.cmdhelper.get_parent_dev",
        _get_parent_dev,
    )
    monkeypatch.setattr(
        "otaclient.boot_control._rpi_boot.cmdhelper.get_device_tree",
        _get_device_tree,
    )
    monkeypatch.setattr(
        "otaclient.boot_control._rpi_boot.cmdhelper.get_attrs_by_dev",
        _get_attrs_by_dev,
    )
    monkeypatch.setattr(
        "otaclient.boot_control._rpi_boot.cmdhelper.is_target_mounted",
        lambda *a, **kw: system_boot_mounted,
    )
    monkeypatch.setattr(
        "otaclient.boot_control._rpi_boot.cmdhelper.mount",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "otaclient.boot_control._rpi_boot.cmdhelper.umount",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "otaclient.boot_control._rpi_boot.cmdhelper.set_ext4_fslabel",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "otaclient.boot_control._rpi_boot.cmdhelper.reboot",
        lambda *a, **kw: None,
    )

    # _env probes — controller never runs as a dynamic client in these tests
    monkeypatch.setattr(
        "otaclient.boot_control._rpi_boot._env.get_dynamic_client_chroot_path",
        lambda: None,
    )
    monkeypatch.setattr(
        "otaclient.boot_control._ota_status_control._env.is_running_as_downloaded_dynamic_app",
        lambda: False,
    )
    monkeypatch.setattr(
        "otaclient.boot_control._slot_mnt_helper._env.is_dynamic_client_running",
        lambda: False,
    )

    # SlotMountHelper registers atexit umount handlers — suppress to avoid
    # firing on real paths during interpreter shutdown
    monkeypatch.setattr(
        "otaclient.boot_control._slot_mnt_helper.atexit.register",
        lambda *a, **kw: None,
    )


@pytest.fixture
def mock_rpi_boot_cmds_slot_a(monkeypatch, system_boot_dir: Path):
    """Active slot is slot_a, system-boot mounted, fslabel matches slot id."""
    _setup_system_boot_dir(system_boot_dir)
    _install_cmdhelper_mocks(
        monkeypatch, active_slot_dev=SLOT_A_DEV, active_fslabel=SLOT_A
    )


@pytest.fixture
def mock_rpi_boot_cmds_slot_b(monkeypatch, system_boot_dir: Path):
    """Active slot is slot_b (used for first-reboot finalize-switch-boot test)."""
    _setup_system_boot_dir(system_boot_dir)
    _install_cmdhelper_mocks(
        monkeypatch, active_slot_dev=SLOT_B_DEV, active_fslabel=SLOT_B
    )


@pytest.fixture
def mock_rpi_internal(mocker: MockerFixture):
    """Stub out _RPIBootControl methods that touch real subprocess/firmware.

    update_firmware: simulates flash-kernel by moving the active slot's
    /boot/{vmlinuz,initrd.img} to /boot/firmware/{vmlinuz,initrd.img}_<standby>.
    reboot_tryboot: no-op.
    """
    update_firmware_mock = mocker.MagicMock()
    reboot_tryboot_mock = mocker.MagicMock()
    mocker.patch(
        "otaclient.boot_control._rpi_boot._RPIBootControl.update_firmware",
        update_firmware_mock,
    )
    mocker.patch(
        "otaclient.boot_control._rpi_boot._RPIBootControl.reboot_tryboot",
        reboot_tryboot_mock,
    )
    return {
        "update_firmware": update_firmware_mock,
        "reboot_tryboot": reboot_tryboot_mock,
    }


#
# ------------ Helpers for building a controller that is mid-update ------------ #
#
def _build_controller_with_mocked_mounts(
    mocker: MockerFixture,
) -> tuple[RPIBootController, dict]:
    """Construct an RPIBootController and stub the SlotMountHelper syscalls.

    Mirrors the grub integration test pattern: only methods that perform real
    syscalls (prepare_standby_dev / mount_standby / mount_active / umount_all)
    are mocked; in-process methods like preserve_ota_folder_to_standby keep
    running on the tmp_path filesystem.
    """
    controller = RPIBootController()
    mounts = {
        "prepare_standby_dev": mocker.MagicMock(),
        "mount_standby": mocker.MagicMock(),
        "mount_active": mocker.MagicMock(),
        "umount_all": mocker.MagicMock(),
    }
    controller._mp_control.prepare_standby_dev = mounts["prepare_standby_dev"]
    controller._mp_control.mount_standby = mounts["mount_standby"]
    controller._mp_control.mount_active = mounts["mount_active"]
    controller._mp_control.umount_all = mounts["umount_all"]
    return controller, mounts


#
# ------------ Startup tests ------------ #
#
class TestRPIBootControllerStartup:
    """Controller startup on slot_a with various initial conditions."""

    @pytest.fixture(autouse=True)
    def setup_env(
        self, redirect_rpi_paths, mock_rpi_boot_cmds_slot_a, mock_rpi_internal
    ):
        """Compose all mocks and path redirections."""

    def test_startup_ota_status_success(self, slot_a_mp: Path):
        """SUCCESS state: controller initializes, status preserved."""
        ota_status_dir = _setup_slot_a_active(slot_a_mp, ota_status=OTAStatus.SUCCESS)

        controller = RPIBootController()

        assert controller.get_booted_ota_status() == OTAStatus.SUCCESS
        assert (ota_status_dir / "status").read_text() == OTAStatus.SUCCESS
        assert (ota_status_dir / "slot_in_use").read_text() == SLOT_A
        assert controller.load_version() == CURRENT_VERSION

    def test_startup_resolves_ab_layout(self, slot_a_mp: Path):
        """Controller correctly identifies active=slot_a, standby=slot_b."""
        _setup_slot_a_active(slot_a_mp)

        controller = RPIBootController()

        assert controller._rpiboot_control.active_slot == SLOT_A
        assert controller._rpiboot_control.standby_slot == SLOT_B
        assert controller._rpiboot_control.active_slot_dev == SLOT_A_DEV
        assert controller._rpiboot_control.standby_slot_dev == SLOT_B_DEV

    def test_startup_clears_legacy_switch_boot_flag(
        self, slot_a_mp: Path, system_boot_dir: Path
    ):
        """Legacy switch-boot flag file is removed on every successful init."""
        _setup_slot_a_active(slot_a_mp)
        flag = system_boot_dir / _rpi_boot.boot_cfg.SWITCH_BOOT_FLAG_FILE
        flag.write_text("")
        assert flag.exists()

        RPIBootController()

        assert not flag.exists()

    @pytest.mark.parametrize(
        "missing",
        [
            pytest.param(f"{CONFIG_TXT}{SEP_CHAR}{SLOT_A}", id="missing-active-config"),
            pytest.param(
                f"{CMDLINE_TXT}{SEP_CHAR}{SLOT_A}", id="missing-active-cmdline"
            ),
            pytest.param(
                f"{CONFIG_TXT}{SEP_CHAR}{SLOT_B}", id="missing-standby-config"
            ),
            pytest.param(
                f"{CMDLINE_TXT}{SEP_CHAR}{SLOT_B}", id="missing-standby-cmdline"
            ),
        ],
    )
    def test_startup_missing_boot_files_raises(
        self, slot_a_mp: Path, system_boot_dir: Path, missing: str
    ):
        """Any missing required boot file aborts startup with the wrapped error."""
        _setup_slot_a_active(slot_a_mp)
        (system_boot_dir / missing).unlink()

        with pytest.raises(BootControlStartupFailed):
            RPIBootController()

    def test_startup_corrects_fslabel_mismatch(
        self, monkeypatch, slot_a_mp: Path, system_boot_dir: Path, mocker: MockerFixture
    ):
        """If active slot's fslabel != slot id, set_ext4_fslabel is invoked."""
        _setup_slot_a_active(slot_a_mp)

        # Override the LABEL probe to return a stale label
        def _stale_label(token, dev, *_, **__):
            if token == "LABEL" and dev == SLOT_A_DEV:
                return "stale_label"
            return ""

        monkeypatch.setattr(
            "otaclient.boot_control._rpi_boot.cmdhelper.get_attrs_by_dev",
            _stale_label,
        )
        set_label_mock = mocker.MagicMock()
        monkeypatch.setattr(
            "otaclient.boot_control._rpi_boot.cmdhelper.set_ext4_fslabel",
            set_label_mock,
        )

        RPIBootController()

        set_label_mock.assert_called_once_with(SLOT_A_DEV, SLOT_A)

    def test_startup_mounts_system_boot_when_unmounted(
        self, monkeypatch, slot_a_mp: Path, mocker: MockerFixture
    ):
        """If system-boot is not mounted, the controller mounts it."""
        _setup_slot_a_active(slot_a_mp)

        monkeypatch.setattr(
            "otaclient.boot_control._rpi_boot.cmdhelper.is_target_mounted",
            lambda *a, **kw: False,
        )
        mount_mock = mocker.MagicMock()
        monkeypatch.setattr(
            "otaclient.boot_control._rpi_boot.cmdhelper.mount", mount_mock
        )

        RPIBootController()

        # mount called with the system-boot partition (sda1)
        mount_mock.assert_called_once()
        args, kwargs = mount_mock.call_args
        assert args[0] == SYSTEM_BOOT_DEV


#
# ------------ Pre-update tests ------------ #
#
class TestRPIBootControllerPreUpdate:
    """Pre-update template method and RPI-specific prepare_standby hook."""

    @pytest.fixture(autouse=True)
    def setup_env(
        self, redirect_rpi_paths, mock_rpi_boot_cmds_slot_a, mock_rpi_internal
    ):
        """Compose all mocks and path redirections."""

    @pytest.mark.parametrize("erase_standby", [True, False])
    def test_pre_update_marks_active_failure_and_prepares_standby(
        self, slot_a_mp: Path, erase_standby: bool, mocker: MockerFixture
    ):
        """pre_update sets active to FAILURE/slot_in_use=standby and forwards
        the standby fslabel to prepare_standby_dev."""
        ota_status_dir = _setup_slot_a_active(slot_a_mp)
        controller, mounts = _build_controller_with_mocked_mounts(mocker)

        controller.pre_update(standby_as_ref=False, erase_standby=erase_standby)

        # Active slot's OTA status moved to FAILURE / slot_in_use=standby
        assert (ota_status_dir / "status").read_text() == OTAStatus.FAILURE
        assert (ota_status_dir / "slot_in_use").read_text() == SLOT_B

        # RPI hook: prepare_standby_dev called with fslabel=standby_slot
        mounts["prepare_standby_dev"].assert_called_once_with(
            erase_standby=erase_standby, fslabel=SLOT_B
        )
        mounts["mount_standby"].assert_called_once()
        mounts["mount_active"].assert_called_once()

    def test_pre_update_call_order(self, slot_a_mp: Path, mocker: MockerFixture):
        """Template method executes steps in the correct order."""
        _setup_slot_a_active(slot_a_mp)
        controller, mounts = _build_controller_with_mocked_mounts(mocker)

        call_order: list[str] = []

        original_pre_update_current = controller._ota_status_control.pre_update_current

        def tracked_pre_update_current():
            call_order.append("pre_update_current")
            original_pre_update_current()

        controller._ota_status_control.pre_update_current = tracked_pre_update_current

        mounts["prepare_standby_dev"].side_effect = lambda **_: call_order.append(
            "prepare_standby_dev"
        )
        mounts["mount_standby"].side_effect = lambda: call_order.append("mount_standby")
        mounts["mount_active"].side_effect = lambda: call_order.append("mount_active")

        controller.pre_update(standby_as_ref=False, erase_standby=True)

        assert call_order == [
            "pre_update_current",
            "prepare_standby_dev",
            "mount_standby",
            "mount_active",
        ]

    def test_pre_update_wraps_exception(self, slot_a_mp: Path, mocker: MockerFixture):
        """Exceptions from hooks are wrapped in BootControlPreUpdateFailed."""
        _setup_slot_a_active(slot_a_mp)
        controller, mounts = _build_controller_with_mocked_mounts(mocker)
        mounts["prepare_standby_dev"].side_effect = RuntimeError("disk error")

        with pytest.raises(BootControlPreUpdateFailed):
            controller.pre_update(standby_as_ref=False, erase_standby=True)


#
# ------------ Post-update tests ------------ #
#
class TestRPIBootControllerPostUpdate:
    """post_update template method and RPI-specific platform hooks."""

    @pytest.fixture(autouse=True)
    def setup_env(
        self, redirect_rpi_paths, mock_rpi_boot_cmds_slot_a, mock_rpi_internal
    ):
        """Compose all mocks and path redirections."""

    def _setup_post_update_state(
        self, slot_a_mp: Path, slot_b_mp: Path, mocker: MockerFixture
    ):
        """Initial state simulates: __init__ (SUCCESS) + pre_update done +
        OTA image already written to standby rootfs.

        Returns (controller, mounts, slot_b_ota_status_dir).
        """
        _setup_slot_a_active(slot_a_mp)

        # standby rootfs prepared with /etc and /boot/ota
        (slot_b_mp / "etc").mkdir(parents=True, exist_ok=True)
        (slot_b_mp / "boot" / "ota").mkdir(parents=True, exist_ok=True)

        # simulate per-slot ota config files copied during the OTA into /boot/ota
        # on the active slot — preserve_ota_folder_to_standby will copy them over
        (slot_a_mp / "boot" / "ota" / "proxy_info.yaml").write_text("proxy")
        (slot_a_mp / "boot" / "ota" / "ecu_info.yaml").write_text("ecu")

        controller, mounts = _build_controller_with_mocked_mounts(mocker)

        slot_b_ota_status_dir = slot_b_mp / Path(
            _rpi_boot.boot_cfg.OTA_STATUS_DIR
        ).relative_to("/")
        return controller, mounts, slot_b_ota_status_dir

    def test_post_update_e2e(
        self,
        slot_a_mp: Path,
        slot_b_mp: Path,
        system_boot_dir: Path,
        mocker: MockerFixture,
        mock_rpi_internal,
    ):
        """Run the full post_update workflow and verify all final state."""
        controller, mounts, slot_b_ota_status_dir = self._setup_post_update_state(
            slot_a_mp, slot_b_mp, mocker
        )

        controller.post_update(update_version=UPDATE_VERSION)

        # --- OTA status files on standby slot ---
        assert (slot_b_ota_status_dir / "status").read_text() == OTAStatus.UPDATING
        assert (slot_b_ota_status_dir / "slot_in_use").read_text() == SLOT_B
        assert (slot_b_ota_status_dir / "version").read_text() == UPDATE_VERSION

        # --- preserve_ota_folder_to_standby copied /boot/ota across ---
        assert (slot_b_mp / "boot" / "ota" / "proxy_info.yaml").read_text() == "proxy"
        assert (slot_b_mp / "boot" / "ota" / "ecu_info.yaml").read_text() == "ecu"

        # --- standby fstab written with the standby slot's fslabel ---
        fstab_content = (slot_b_mp / "etc" / "fstab").read_text()
        expected_fstab = Template(_rpi_boot._FSTAB_TEMPLATE_STR).substitute(
            rootfs_fslabel=SLOT_B
        )
        assert fstab_content == expected_fstab

        # --- update_firmware invoked with target=standby ---
        mock_rpi_internal["update_firmware"].assert_called_once()
        kwargs = mock_rpi_internal["update_firmware"].call_args.kwargs
        assert kwargs["target_slot"] == SLOT_B
        assert Path(kwargs["target_slot_mp"]) == slot_b_mp

        # --- tryboot.txt prepared from standby's config.txt ---
        assert (system_boot_dir / TRYBOOT_TXT).read_text() == CONFIG_TXT_SLOT_B
        # active config.txt left unchanged at this stage
        assert (system_boot_dir / CONFIG_TXT).read_text() == CONFIG_TXT_SLOT_A

        # --- umount_all called once for cleanup ---
        mounts["umount_all"].assert_called_once_with(ignore_error=True)

    def test_post_update_wraps_exception(
        self, slot_a_mp: Path, slot_b_mp: Path, mocker: MockerFixture
    ):
        """Exceptions from hooks are wrapped in BootControlPostUpdateFailed."""
        controller, _, _ = self._setup_post_update_state(slot_a_mp, slot_b_mp, mocker)

        # remove standby /etc so write_str_to_file_atomic for fstab fails
        # (atomic write uses a tmp file in the parent directory)
        import shutil

        shutil.rmtree(slot_b_mp / "etc")

        with pytest.raises(BootControlPostUpdateFailed):
            controller.post_update(update_version=UPDATE_VERSION)


#
# ------------ finalizing_update test ------------ #
#
class TestRPIBootControllerFinalizingUpdate:
    """finalizing_update reboots into tryboot."""

    @pytest.fixture(autouse=True)
    def setup_env(
        self, redirect_rpi_paths, mock_rpi_boot_cmds_slot_a, mock_rpi_internal
    ):
        """Compose all mocks and path redirections."""

    def test_finalizing_update_calls_reboot_tryboot(
        self, slot_a_mp: Path, mocker: MockerFixture, mock_rpi_internal
    ):
        _setup_slot_a_active(slot_a_mp)
        controller, _ = _build_controller_with_mocked_mounts(mocker)

        controller.finalizing_update()

        mock_rpi_internal["reboot_tryboot"].assert_called_once()


#
# ------------ Finalize switch boot ------------ #
#
# First reboot after a slot_a -> slot_b update.
class TestRPIBootControllerFinalizeSwitchBoot:
    """Controller startup on slot_b with status=UPDATING triggers finalization."""

    @pytest.fixture(autouse=True)
    def setup_env(
        self,
        redirect_rpi_paths_slot_b_active,
        mock_rpi_boot_cmds_slot_b,
        mock_rpi_internal,
    ):
        """Compose all mocks for the slot_b-active scenario."""

    def test_first_reboot_finalizes_switch_boot(
        self, slot_b_mp: Path, system_boot_dir: Path
    ):
        """status=UPDATING + slot_in_use=slot_b on slot_b triggers
        finalize_switching_boot, which:
          - replaces /boot/firmware/config.txt with config.txt_<active>
          - replaces /boot/firmware/tryboot.txt with config.txt_<standby>
          - flips status to SUCCESS
        """
        ota_status_dir = _setup_slot_b_post_switch(slot_b_mp)

        # leave a leftover .bak file from flash-kernel; finalize should remove it
        bak_file = system_boot_dir / "leftover.bak"
        bak_file.write_text("old")

        controller = RPIBootController()

        # Status flipped to SUCCESS
        assert controller.get_booted_ota_status() == OTAStatus.SUCCESS
        assert (ota_status_dir / "status").read_text() == OTAStatus.SUCCESS

        # config.txt now mirrors slot_b (the new active), tryboot.txt mirrors slot_a
        assert (system_boot_dir / CONFIG_TXT).read_text() == CONFIG_TXT_SLOT_B
        assert (system_boot_dir / TRYBOOT_TXT).read_text() == CONFIG_TXT_SLOT_A

        # leftover .bak files are cleaned up
        assert not bak_file.exists()
