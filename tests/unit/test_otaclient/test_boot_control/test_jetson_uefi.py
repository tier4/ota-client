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


from __future__ import annotations

import contextlib
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from otaclient.boot_control import _jetson_uefi
from otaclient.boot_control._jetson_common import (
    BSPVersion,
    NVBootctrlExecError,
    SlotID,
)
from otaclient.boot_control._jetson_uefi import (
    JetsonUEFIBootControl,
    JetsonUEFIBootControlError,
    L4TLauncherBSPVersionControl,
    NVBootctrlJetsonUEFI,
    RootfsBootStatusControl,
    _detect_esp_dev,
    _detect_ota_bootdev_is_qspi,
    _l4tlauncher_version_control,
)
from otaclient.boot_control.configs import jetson_uefi_cfg as boot_cfg

MODULE = _jetson_uefi.__name__


class TestNVBootctrlJetsonUEFI:
    @pytest.mark.parametrize(
        "_input, expected",
        (
            (
                """\
Current version: 35.4.1
Capsule update status: 1
Current bootloader slot: A
Active bootloader slot: A
num_slots: 2
slot: 0,             status: normal
slot: 1,             status: normal
             """,
                BSPVersion.parse("35.4.1"),
            ),
            (
                """\
Current version: 36.3.0
Capsule update status: 0
Current bootloader slot: A
Active bootloader slot: A
num_slots: 2
slot: 0,             status: normal
slot: 1,             status: normal
             """,
                BSPVersion.parse("36.3.0"),
            ),
            (
                """\
Current version: 0.0.0
Current bootloader slot: A
Active bootloader slot: A
num_slots: 2
slot: 0,             status: normal
slot: 1,             status: normal
             """,
                NVBootctrlExecError(),
            ),
        ),
    )
    def test_get_current_fw_bsp_version(
        self, _input: str, expected: BSPVersion | Exception, mocker: MockerFixture
    ):
        mocker.patch.object(
            NVBootctrlJetsonUEFI,
            "dump_slots_info",
            mocker.MagicMock(return_value=_input),
        )

        if isinstance(expected, Exception):
            with pytest.raises(type(expected)):
                NVBootctrlJetsonUEFI.get_current_fw_bsp_version()
        else:
            assert NVBootctrlJetsonUEFI.get_current_fw_bsp_version() == expected

    @pytest.mark.parametrize(
        "_input, expected",
        (
            (
                """\
Current version: 35.4.1
Capsule update status: 1
Current bootloader slot: A
Active bootloader slot: A
num_slots: 2
slot: 0,             status: normal
slot: 1,             status: normal
             """,
                SlotID("0"),
            ),
            (
                """\
Current version: 35.4.1
Capsule update status: 1
Current bootloader slot: A
Active bootloader slot: B
num_slots: 2
slot: 0,             status: normal
slot: 1,             status: normal
             """,
                SlotID("1"),
            ),
        ),
    )
    def test_get_active_bootloader_slot(
        self, _input: str, expected: SlotID, mocker: MockerFixture
    ):
        mocker.patch.object(
            NVBootctrlJetsonUEFI,
            "dump_slots_info",
            mocker.MagicMock(return_value=_input),
        )
        assert NVBootctrlJetsonUEFI.get_active_bootloader_slot() == expected


@pytest.mark.parametrize(
    "all_esp_devs, boot_parent_devpath, expected",
    (
        (
            ["/dev/mmcblk0p41", "/dev/nvme0n1p41"],
            "/dev/nvme0n1",
            "/dev/nvme0n1p41",
        ),
        (
            ["/dev/mmcblk0p41"],
            "/dev/mmcblk0",
            "/dev/mmcblk0p41",
        ),
        (
            ["/dev/sda23", "/dev/mmcblk0p41"],
            "/dev/nvme0n1",
            JetsonUEFIBootControlError(),
        ),
        (
            [],
            "/dev/nvme0n1",
            JetsonUEFIBootControlError(),
        ),
    ),
)
def test__detect_esp_dev(
    all_esp_devs, boot_parent_devpath, expected, mocker: MockerFixture
):
    mocker.patch(
        f"{MODULE}.cmdhelper.get_dev_by_token",
        mocker.MagicMock(return_value=all_esp_devs),
    )

    if isinstance(expected, Exception):
        with pytest.raises(type(expected)):
            _detect_esp_dev(boot_parent_devpath)
    else:
        assert _detect_esp_dev(boot_parent_devpath) == expected


@pytest.mark.parametrize(
    "nvbootctrl_conf, expected",
    (
        (
            """\
TNSPEC 3701-500-0005-A.0-1-0-cti-orin-agx-agx201-00-
TEGRA_CHIPID 0x23
TEGRA_OTA_BOOT_DEVICE /dev/mtdblock0
TEGRA_OTA_GPT_DEVICE /dev/mtdblock0
""",
            True,
        ),
        (
            """\
TNSPEC 2888-400-0004-M.0-1-2-jetson-xavier-rqx580-
TEGRA_CHIPID 0x19
TEGRA_OTA_BOOT_DEVICE /dev/mmcblk0boot0
TEGRA_OTA_GPT_DEVICE /dev/mmcblk0boot1
""",
            False,
        ),
        ("", None),
    ),
)
def test__detect_ota_bootdev_is_qspi(nvbootctrl_conf, expected):
    assert _detect_ota_bootdev_is_qspi(nvbootctrl_conf) is expected


class TestL4TLauncherBSPVersionControl:
    @pytest.mark.parametrize(
        "_in, expected",
        _test_case := (
            (
                "R35.4.1:ac17457772666351154a5952e3b87851a6398da2afcf3a38bedfc0925760bb0e",
                L4TLauncherBSPVersionControl(
                    bsp_ver=BSPVersion(35, 4, 1),
                    sha256_digest="ac17457772666351154a5952e3b87851a6398da2afcf3a38bedfc0925760bb0e",
                ),
            ),
            (
                "R35.5.0:3928e0feb84e37db87dc6705a02eec95a6fca2dccd3467b61fa66ed8e1046b67",
                L4TLauncherBSPVersionControl(
                    bsp_ver=BSPVersion(35, 5, 0),
                    sha256_digest="3928e0feb84e37db87dc6705a02eec95a6fca2dccd3467b61fa66ed8e1046b67",
                ),
            ),
            (
                "R36.3.0:b14fa3623f4078d05573d9dcf2a0b46ea2ae07d6b75d9843f9da6ff24db13718",
                L4TLauncherBSPVersionControl(
                    bsp_ver=BSPVersion(36, 3, 0),
                    sha256_digest="b14fa3623f4078d05573d9dcf2a0b46ea2ae07d6b75d9843f9da6ff24db13718",
                ),
            ),
        ),
    )
    def test_parse(self, _in, expected):
        assert L4TLauncherBSPVersionControl.parse(_in) == expected

    @pytest.mark.parametrize(
        "to_be_dumped, expected",
        tuple((entry[1], entry[0]) for entry in _test_case),
    )
    def test_dump(self, to_be_dumped: L4TLauncherBSPVersionControl, expected: str):
        assert to_be_dumped.dump() == expected


@pytest.mark.parametrize(
    "ver_ctrl, l4tlauncher_digest, current_bsp_ver, expected_bsp_ver, expected_ver_ctrl",
    (
        # valid version file, hash matched
        (
            "R36.3.0:b14fa3623f4078d05573d9dcf2a0b46ea2ae07d6b75d9843f9da6ff24db13718",
            "b14fa3623f4078d05573d9dcf2a0b46ea2ae07d6b75d9843f9da6ff24db13718",
            BSPVersion(1, 2, 3),
            BSPVersion(36, 3, 0),
            "R36.3.0:b14fa3623f4078d05573d9dcf2a0b46ea2ae07d6b75d9843f9da6ff24db13718",
        ),
        # no version file, lookup table hit
        (
            "invalid_version_file",
            "b14fa3623f4078d05573d9dcf2a0b46ea2ae07d6b75d9843f9da6ff24db13718",
            BSPVersion(1, 2, 3),
            BSPVersion(36, 3, 0),
            "R36.3.0:b14fa3623f4078d05573d9dcf2a0b46ea2ae07d6b75d9843f9da6ff24db13718",
        ),
        # valid version file, hash mismatched, use slot BSP version
        (
            "R36.3.0:b14fa3623f4078d05573d9dcf2a0b46ea2ae07d6b75d9843f9da6ff24db13718",
            "hash_mismatched",
            BSPVersion(1, 2, 3),
            BSPVersion(1, 2, 3),
            "R1.2.3:hash_mismatched",
        ),
        # no version file, lookup table unhit
        (
            "invalid_version_file",
            "not_recorded_hash",
            BSPVersion(1, 2, 3),
            BSPVersion(1, 2, 3),
            "R1.2.3:not_recorded_hash",
        ),
    ),
)
def test__l4tlauncher_version_control(
    ver_ctrl,
    l4tlauncher_digest,
    current_bsp_ver,
    expected_bsp_ver,
    expected_ver_ctrl,
    tmp_path: Path,
    mocker: MockerFixture,
):
    ver_control_f = tmp_path / "l4tlauncher_ver_control"
    ver_control_f.write_text(ver_ctrl)

    mocker.patch(
        f"{MODULE}.file_sha256", mocker.MagicMock(return_value=l4tlauncher_digest)
    )

    assert (
        _l4tlauncher_version_control(
            ver_control_f,
            "any",
            current_slot_bsp_ver=current_bsp_ver,
        )
        == expected_bsp_ver
    )
    # ensure the version control file is expected
    assert ver_control_f.read_text() == expected_ver_ctrl


class TestJetsonUEFIBootControlBSPVersionCheck:
    """Test BSP version compatibility check for JetsonUEFIBootControl."""

    @pytest.mark.parametrize(
        "current_bsp_version, download_bsp_version, expected_result",
        (
            # Same generation compatibility: R35 -> R35.x
            (
                "R35.1.0",
                "R35.1.0",
                True,
            ),
            (
                "R35.4.1",
                "R35.1.0",
                False,
            ),
            # Same generation compatibility: R36 -> R36.x
            (
                "R36.1.0",
                "R36.3.0",
                False,
            ),
            (
                "R36.3.0",
                "R36.3.0",
                True,
            ),
            # Cross-generation incompatibility
            (
                "R35.1.0",
                "R36.1.0",
                False,
            ),
            (
                "R36.1.0",
                "R35.1.0",
                False,
            ),
        ),
    )
    def test_check_bsp_version_compatibility(
        self,
        current_bsp_version: str,
        download_bsp_version: str,
        expected_result: bool,
        mocker: MockerFixture,
    ):
        """Test BSP version compatibility checking logic."""
        boot_control = mocker.MagicMock(spec=JetsonUEFIBootControl)

        mock_fw_bsp_ver_control = mocker.MagicMock()
        mock_fw_bsp_ver_control.standby_slot_bsp_ver = BSPVersion.parse(
            current_bsp_version
        )
        mock_fw_bsp_ver_control.current_slot_bsp_ver = BSPVersion.parse(
            current_bsp_version
        )
        boot_control._firmware_bsp_ver_control = mock_fw_bsp_ver_control

        result, _ = JetsonUEFIBootControl.current_slot_bsp_ver_check(
            boot_control, download_bsp_version
        )

        assert result == expected_result

    def test_check_bsp_version_compatibility_with_none_standby_version(
        self, mocker: MockerFixture
    ):
        """Test that uses current slot BSP version when standby slot BSP version is None."""
        boot_control = mocker.MagicMock(spec=JetsonUEFIBootControl)

        # When standby is None, fall back to current slot version (R35.4.1)
        # against download version (R35.4.1) — same generation should match.
        mock_fw_bsp_ver_control = mocker.MagicMock()
        mock_fw_bsp_ver_control.standby_slot_bsp_ver = None
        mock_fw_bsp_ver_control.current_slot_bsp_ver = BSPVersion.parse("R35.4.1")
        boot_control._firmware_bsp_ver_control = mock_fw_bsp_ver_control

        result, _ = JetsonUEFIBootControl.current_slot_bsp_ver_check(
            boot_control,
            "R35.4.1",
        )

        assert result is True


class TestRootfsBootStatusControl:
    """Test the force-reset rootfs unbootable flag mechanism (QSPI only)."""

    def test__reset_slot_boot_flag_writes_payload(
        self, tmp_path: Path, mocker: MockerFixture
    ):
        """The efivar payload is rewritten to the bootable value in-place."""
        var_f = tmp_path / boot_cfg.ROOTFS_STATUS_SLOT_A_EFIVAR
        # pre-populate with a different value of the same length
        var_f.write_bytes(b"\x07\x00\x00\x00\x01\x00\x00\x00")

        # the immutable-flag ioctl only works on real efivarfs, bypass it here
        mocker.patch.object(
            RootfsBootStatusControl,
            "_immutable_flag_cleared",
            return_value=contextlib.nullcontext(),
        )
        mock_sync = mocker.patch(f"{MODULE}.os.sync")

        RootfsBootStatusControl._reset_slot_boot_flag("RootfsStatusSlotA", var_f)

        assert var_f.read_bytes() == boot_cfg.RESET_ROOTFS_STATUS_PAYLOAD
        assert var_f.read_bytes() == b"\x07\x00\x00\x00\x00\x00\x00\x00"
        mock_sync.assert_called_once()

    def test__reset_slot_boot_flag_missing_efivar(
        self, tmp_path: Path, mocker: MockerFixture
    ):
        """Missing efivar is skipped without touching the immutable flag."""
        var_f = tmp_path / "does_not_exist"
        mock_immutable = mocker.patch.object(
            RootfsBootStatusControl, "_immutable_flag_cleared"
        )

        # should not raise
        RootfsBootStatusControl._reset_slot_boot_flag("RootfsStatusSlotA", var_f)
        mock_immutable.assert_not_called()

    def test__reset_slot_boot_flag_swallow_error(
        self, tmp_path: Path, mocker: MockerFixture
    ):
        """Any failure is best-effort: logged and swallowed, never raised."""
        var_f = tmp_path / boot_cfg.ROOTFS_STATUS_SLOT_A_EFIVAR
        var_f.write_bytes(b"\x00" * 8)
        mocker.patch.object(
            RootfsBootStatusControl,
            "_immutable_flag_cleared",
            side_effect=PermissionError("no permission"),
        )

        # should not propagate the exception
        RootfsBootStatusControl._reset_slot_boot_flag("RootfsStatusSlotA", var_f)

    def test_reset_unbootable_flag_resets_both_slots(self, mocker: MockerFixture):
        """Both slot A and slot B are reset, within the efivarfs mount."""
        mocker.patch(
            f"{MODULE}._ensure_efivarfs_mounted",
            return_value=contextlib.nullcontext(),
        )
        mock_reset_slot = mocker.patch.object(
            RootfsBootStatusControl, "_reset_slot_boot_flag"
        )

        RootfsBootStatusControl.reset_unbootable_flag()

        assert mock_reset_slot.call_count == 2
        reset_slot_names = [c.args[0] for c in mock_reset_slot.call_args_list]
        assert reset_slot_names == ["RootfsStatusSlotA", "RootfsStatusSlotB"]

    @pytest.mark.parametrize(
        "device_uses_qspi, expect_reset",
        (
            # QSPI device: reset unbootable flag
            (True, True),
            # internal emmc device: DO NOT reset
            (False, False),
            # unknown/undetectable OTA_BOOTDEV: DO NOT reset
            (None, False),
        ),
    )
    def test__post_update_reset_flag_gating(
        self,
        device_uses_qspi: bool | None,
        expect_reset: bool,
        mocker: MockerFixture,
    ):
        """Only QSPI-equipped devices force-reset the unbootable flag."""
        boot_control = mocker.MagicMock(spec=JetsonUEFIBootControl)
        boot_control._uefi_control = mocker.MagicMock()
        boot_control._uefi_control.nvbootctrl_conf = "dummy nvbootctrl conf"
        # avoid the active-vs-current bootloader slot mismatch branch
        boot_control._uefi_control.active_bootloader_slot = None
        boot_control._uefi_control.external_rootfs = False
        boot_control._mp_control = mocker.MagicMock()
        boot_control._firmware_update.return_value = False

        # patch collaborators unrelated to the gating under test
        mocker.patch(f"{MODULE}.update_standby_slot_extlinux_cfg")
        mocker.patch(f"{MODULE}.preserve_ota_config_files_to_standby")
        mocker.patch(f"{MODULE}.replace_root", return_value="/dummy")
        mocker.patch(f"{MODULE}.NVBootctrlJetsonUEFI")
        mocker.patch(f"{MODULE}._env.get_dynamic_client_chroot_path", return_value=None)
        mocker.patch(
            f"{MODULE}._detect_ota_bootdev_is_qspi", return_value=device_uses_qspi
        )
        mock_reset = mocker.patch.object(
            RootfsBootStatusControl, "reset_unbootable_flag"
        )

        JetsonUEFIBootControl._post_update_platform_specific(
            boot_control, update_version="v1"
        )

        if expect_reset:
            mock_reset.assert_called_once()
        else:
            mock_reset.assert_not_called()
