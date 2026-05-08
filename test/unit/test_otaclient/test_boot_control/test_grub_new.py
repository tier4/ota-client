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

import json
from pathlib import Path

import pytest

from otaclient.boot_control._grub_common import (
    BootFiles,
    GrubBootControllerError,
    OTAManagedCfg,
    OTASlotBootID,
    PartitionInfo,
)
from otaclient.boot_control._grub_new import (
    DEV_PATH_PA,
    GRUB_BLACKLIST_OPTIONS,
    GRUB_DEFAULT_OPTIONS,
    INITRD_PA_MULTILINE,
    INITRD_PREFIX,
    LINUX_PA_MULTILINE,
    LINUX_VERSION_PA,
    MENUENTRY_HEAD_PA,
    MENUENTRY_ID_PA,
    MENUENTRY_TITLE_PA,
    VMLINUZ_PREFIX,
    ABPartitionDetector,
    _BootMenuEntry,
    _detect_boot_control_setup_case,
    _GrubBootControl,
    _GrubBootControlSetupCase,
    _GrubBootHelperFuncs,
    iter_menuentries,
)

from .conftest import (
    EXPECTED_OTA_MANAGED_DEFAULT,
    GRUB_CFG,
    GRUB_DEFAULT,
    GRUB_DEFAULT_WITH_BLACKLISTED,
    GRUB_DEFAULT_WITH_DUPLICATES,
    OLD_GRUB_BOOT_CFG,
    OTA_MANAGED_GRUB_CFG,
)

# Shared test data: (grub_cfg, kernel_ver) for the standard grub.cfg test file.
_GRUB_CFG_KERNEL_VER = (GRUB_CFG, "5.19.0-50-generic")


class TestDevPathPattern:
    @pytest.mark.parametrize(
        "dev_path, expected_dev_name, expected_partition_id",
        [
            pytest.param("/dev/sda1", "sda", "1", id="sda1"),
            pytest.param("/dev/sda2", "sda", "2", id="sda2"),
            pytest.param("/dev/sdb3", "sdb", "3", id="sdb3"),
            pytest.param("/dev/nvme0n1p3", "nvme0n1p", "3", id="nvme0n1p3"),
            pytest.param("/dev/nvme0n1p4", "nvme0n1p", "4", id="nvme0n1p4"),
            pytest.param("/dev/mmcblk0p1", "mmcblk0p", "1", id="mmcblk0p1"),
        ],
    )
    def test_matches_valid_dev_paths(
        self, dev_path: str, expected_dev_name: str, expected_partition_id: str
    ):
        ma = DEV_PATH_PA.match(dev_path)
        assert ma is not None
        assert ma.group("dev_name") == expected_dev_name
        assert ma.group("partition_id") == expected_partition_id

    @pytest.mark.parametrize(
        "dev_path",
        [
            pytest.param("/dev/sda", id="no_partition_id"),
            pytest.param("sda1", id="missing_dev_prefix"),
            pytest.param("/dev/", id="no_device_name"),
            pytest.param("/dev/dm-0", id="device_mapper_with_hyphen"),
        ],
    )
    def test_rejects_invalid_dev_paths(self, dev_path: str):
        assert DEV_PATH_PA.match(dev_path) is None


class TestMenuentryRegexPatterns:
    @pytest.mark.parametrize(
        "line, should_match",
        [
            pytest.param("menuentry 'Ubuntu' {", True, id="simple"),
            pytest.param("  menuentry 'Ubuntu' {", True, id="leading_spaces"),
            pytest.param("\tmenuentry 'Ubuntu' {", True, id="leading_tab"),
            pytest.param("# menuentry 'Ubuntu' {", False, id="commented_out"),
            pytest.param("submenuentry 'Ubuntu' {", False, id="not_menuentry"),
        ],
    )
    def test_menuentry_head_pattern(self, line: str, should_match: bool):
        result = MENUENTRY_HEAD_PA.search(line)
        assert (result is not None) == should_match

    @pytest.mark.parametrize(
        "line, expected_fpath",
        [
            pytest.param(
                "\tlinux\t/vmlinuz-6.11.0-29-generic root=UUID=3244f06d-e6f4-42f8-a9e3-c4a72a672b90 ro",
                "/vmlinuz-6.11.0-29-generic",
                id="tab_separated",
            ),
            pytest.param(
                "\tlinux /boot/vmlinuz-5.15.0-100-generic root=UUID=3244f06d-e6f4-42f8-a9e3-c4a72a672b90 ro",
                "/boot/vmlinuz-5.15.0-100-generic",
                id="space_separated",
            ),
            pytest.param(
                "\tlinux  /ota-slot_a/vmlinuz-6.11.0-29-generic root=UUID=3244f06d-e6f4-42f8-a9e3-c4a72a672b90 ro",
                "/ota-slot_a/vmlinuz-6.11.0-29-generic",
                id="slot_prefixed",
            ),
        ],
    )
    def test_linux_pa_multiline(self, line: str, expected_fpath: str):
        ma = LINUX_PA_MULTILINE.search(line)
        assert ma is not None
        assert ma.group("linux_fpath") == expected_fpath

    @pytest.mark.parametrize(
        "fpath, expected_ver",
        [
            pytest.param(
                "/vmlinuz-6.11.0-29-generic", "6.11.0-29-generic", id="standard"
            ),
            pytest.param(
                "/boot/vmlinuz-5.15.0-100-generic",
                "5.15.0-100-generic",
                id="with_boot_prefix",
            ),
            pytest.param(
                "/ota-slot_a/vmlinuz-6.11.0-29-generic",
                "6.11.0-29-generic",
                id="with_slot_prefix",
            ),
        ],
    )
    def test_linux_version_pattern(self, fpath: str, expected_ver: str):
        ma = LINUX_VERSION_PA.search(fpath)
        assert ma is not None
        assert ma.group("ver") == expected_ver

    @pytest.mark.parametrize(
        "line, expected_fpath",
        [
            pytest.param(
                "\tinitrd\t/initrd.img-6.11.0-29-generic",
                "/initrd.img-6.11.0-29-generic",
                id="tab_separated",
            ),
            pytest.param(
                "  initrd /ota-slot_b/initrd.img-5.15.0-100-generic",
                "/ota-slot_b/initrd.img-5.15.0-100-generic",
                id="slot_prefixed",
            ),
        ],
    )
    def test_initrd_pa_multiline(self, line: str, expected_fpath: str):
        ma = INITRD_PA_MULTILINE.search(line)
        assert ma is not None
        assert ma.group("initrd_fpath") == expected_fpath

    @pytest.mark.parametrize(
        "header, expected_title",
        [
            pytest.param(
                "menuentry 'Ubuntu' --class ubuntu --class gnu-linux",
                "'Ubuntu'",
                id="single_quoted",
            ),
            pytest.param(
                'menuentry "Ubuntu 22.04" --class ubuntu gnu-linux',
                '"Ubuntu 22.04"',
                id="double_quoted",
            ),
            pytest.param(
                "menuentry Ubuntu --class ubuntu",
                "Ubuntu",
                id="unquoted",
            ),
        ],
    )
    def test_menuentry_title_pattern(self, header: str, expected_title: str):
        ma = MENUENTRY_TITLE_PA.match(header)
        assert ma is not None
        assert ma.group("entry_title") == expected_title

    @pytest.mark.parametrize(
        "header, expected_id",
        [
            pytest.param(
                "menuentry 'Ubuntu' $menuentry_id_option 'ota-slot_a'",
                "'ota-slot_a'",
                id="single_quoted_id",
            ),
            pytest.param(
                'menuentry "Ubuntu" $menuentry_id_option "gnulinux-advanced"',
                '"gnulinux-advanced"',
                id="double_quoted_id",
            ),
        ],
    )
    def test_menuentry_id_pattern(self, header: str, expected_id: str):
        ma = MENUENTRY_ID_PA.match(header)
        assert ma is not None
        assert ma.group("entry_id") == expected_id


# ============================================================
# iter_menuentries
# ============================================================


class TestIterMenuentries:
    @pytest.mark.parametrize(
        "input_str, expected_count",
        [
            pytest.param(GRUB_CFG, 2, id="grub_cfg_2_entries"),
            pytest.param(OLD_GRUB_BOOT_CFG, 4, id="old_grub_boot_cfg_4_entries"),
            # NOTE: for OTA managed main grub.cfg, no boot entries should be directly written
            pytest.param(OTA_MANAGED_GRUB_CFG, 0, id="ota_managed_cfg_no_entries"),
            pytest.param(
                "### BEGIN ###\nno entries here\n### END ###\n", 0, id="no_entries"
            ),
        ],
    )
    def test_counts_menuentries(self, input_str: str, expected_count: int):
        assert len(list(iter_menuentries(input_str))) == expected_count

    @pytest.mark.parametrize(
        "input_str, error_msg_fragment",
        [
            pytest.param(
                "menuentry 'broken' no brace here\n",
                "no opening brace",
                id="no_opening_brace",
            ),
            pytest.param(
                "menuentry 'broken' {\n  unclosed\n",
                "unclosed braces",
                id="unclosed_braces",
            ),
            pytest.param(
                "menuentry 'broken' {\n  abc \n{ unclosed\n }\n",
                "unclosed braces",
                id="unclosed_braces_2",
            ),
        ],
    )
    def test_raises_on_malformed_entry(self, input_str: str, error_msg_fragment: str):
        with pytest.raises(ValueError, match=error_msg_fragment):
            list(iter_menuentries(input_str))


# ============================================================
# _BootMenuEntry._find_menuentry
# ============================================================


class TestBootMenuEntryFindMenuentry:
    def test_finds_matching_kernel(self):
        result = _BootMenuEntry._find_menuentry(
            GRUB_CFG, kernel_ver="5.19.0-50-generic"
        )
        assert "vmlinuz-5.19.0-50-generic" in result

    def test_raises_on_no_match(self):
        with pytest.raises(ValueError, match="failed to find menuentry"):
            _BootMenuEntry._find_menuentry(GRUB_CFG, kernel_ver="99.99.0-nonexistent")

    def test_skips_entry_without_linux_directive(self):
        """The UEFI Firmware Settings entry in grub.cfg has no linux
        directive, so it should be skipped when searching for a kernel version."""
        result = _BootMenuEntry._find_menuentry(
            GRUB_CFG, kernel_ver="5.19.0-50-generic"
        )
        assert "fwsetup" not in result

    def test_skips_recovery_entry(self):
        """Recovery entries have `recovery` as a boot argument in the linux
        directive (e.g. `linux /vmlinuz-... root=UUID=... ro recovery nomodeset`).
        The skip logic uses LINUX_RECOVERY_PA to match `recovery` as a
        word-boundary argument, not a substring of the file path.
        """
        recovery = (
            "menuentry 'Ubuntu (recovery)' $menuentry_id_option 'recovery' {\n"
            "\tlinux\t/vmlinuz-5.19.0-50-generic root=UUID=abc ro recovery nomodeset\n"
            "\tinitrd\t/initrd.img-5.19.0-50-generic\n"
            "}\n"
        )
        normal = (
            "menuentry 'Ubuntu' $menuentry_id_option 'normal' {\n"
            "\tlinux\t/vmlinuz-5.19.0-50-generic root=UUID=abc ro\n"
            "\tinitrd\t/initrd.img-5.19.0-50-generic\n"
            "}\n"
        )
        combined = recovery + normal
        result = _BootMenuEntry._find_menuentry(
            combined, kernel_ver="5.19.0-50-generic"
        )
        assert "recovery" not in result
        assert "vmlinuz-5.19.0-50-generic" in result

    def test_does_not_skip_recovery_in_path(self):
        """A kernel under a path containing 'recovery' (e.g. /recovery/vmlinuz-...)
        should NOT be skipped if `recovery` is not a boot argument."""
        entry = (
            "menuentry 'Ubuntu' $menuentry_id_option 'normal' {\n"
            "\tlinux\t/recovery/vmlinuz-5.19.0-50-generic root=UUID=abc ro\n"
            "\tinitrd\t/initrd.img-5.19.0-50-generic\n"
            "}\n"
        )
        result = _BootMenuEntry._find_menuentry(entry, kernel_ver="5.19.0-50-generic")
        assert "vmlinuz-5.19.0-50-generic" in result


class TestBootMenuEntryFixupFpath:
    @pytest.mark.parametrize(
        "fpath, slot_id, expected",
        [
            pytest.param(
                "/vmlinuz-6.11.0-29-generic",
                OTASlotBootID.slot_a,
                "/ota-slot_a/vmlinuz-6.11.0-29-generic",
                id="prefix_slot_a",
            ),
            pytest.param(
                "/initrd.img-6.11.0-29-generic",
                OTASlotBootID.slot_b,
                "/ota-slot_b/initrd.img-6.11.0-29-generic",
                id="prefix_slot_b",
            ),
            pytest.param(
                "/ota-slot_a/vmlinuz-6.11.0-29-generic",
                OTASlotBootID.slot_a,
                "/ota-slot_a/vmlinuz-6.11.0-29-generic",
                id="already_prefixed_unchanged",
            ),
        ],
    )
    def test_fixup_fpath(self, fpath: str, slot_id: OTASlotBootID, expected: str):
        result = _BootMenuEntry._fixup_fpath(fpath, slot_id=slot_id)
        assert result == expected


# ============================================================
# _BootMenuEntry._fixup_menuentry
# ============================================================


class TestBootMenuEntryFixupMenuentry:
    """Test _fixup_menuentry using real entries extracted from grub.cfg."""

    @pytest.mark.parametrize(
        ("grub_cfg", "kernel_ver", "slot_boot_id"),
        [
            pytest.param(*_GRUB_CFG_KERNEL_VER, OTASlotBootID.slot_a, id="slot_a"),
            pytest.param(*_GRUB_CFG_KERNEL_VER, OTASlotBootID.slot_b, id="slot_b"),
        ],
    )
    def test_replaces_title_and_id(
        self, grub_cfg: str, kernel_ver: str, slot_boot_id: OTASlotBootID
    ):
        raw_entry = _BootMenuEntry._find_menuentry(grub_cfg, kernel_ver=kernel_ver)
        result = _BootMenuEntry._fixup_menuentry(raw_entry, slot_boot_id=slot_boot_id)
        title_ma = MENUENTRY_TITLE_PA.match(result)
        assert title_ma is not None
        assert title_ma.group("entry_title") == f"'{slot_boot_id}'"

        id_ma = MENUENTRY_ID_PA.match(result)
        assert id_ma is not None
        assert id_ma.group("entry_id") == f"'{slot_boot_id}'"

    @pytest.mark.parametrize(
        ("grub_cfg", "kernel_ver", "slot_boot_id"),
        [
            pytest.param(*_GRUB_CFG_KERNEL_VER, OTASlotBootID.slot_a, id="slot_a"),
            pytest.param(*_GRUB_CFG_KERNEL_VER, OTASlotBootID.slot_b, id="slot_b"),
        ],
    )
    def test_prefixes_linux_and_initrd_paths(
        self, grub_cfg: str, kernel_ver: str, slot_boot_id: OTASlotBootID
    ):
        raw_entry = _BootMenuEntry._find_menuentry(grub_cfg, kernel_ver=kernel_ver)
        result = _BootMenuEntry._fixup_menuentry(raw_entry, slot_boot_id=slot_boot_id)
        linux_ma = LINUX_PA_MULTILINE.search(result)
        assert linux_ma is not None
        assert linux_ma.group("linux_fpath").startswith(f"/{slot_boot_id}/")

        initrd_ma = INITRD_PA_MULTILINE.search(result)
        assert initrd_ma is not None
        assert initrd_ma.group("initrd_fpath").startswith(f"/{slot_boot_id}/")

    @pytest.mark.parametrize(
        ("grub_cfg", "kernel_ver", "slot_boot_id"),
        [
            pytest.param(*_GRUB_CFG_KERNEL_VER, OTASlotBootID.slot_a, id="slot_a"),
            pytest.param(*_GRUB_CFG_KERNEL_VER, OTASlotBootID.slot_b, id="slot_b"),
        ],
    )
    def test_result_is_valid_menuentry(
        self, grub_cfg: str, kernel_ver: str, slot_boot_id: OTASlotBootID
    ):
        """The fixup result should be a valid menuentry that survives a round-trip
        through iter_menuentries and contains the boot echo hint."""
        raw_entry = _BootMenuEntry._find_menuentry(grub_cfg, kernel_ver=kernel_ver)
        result = _BootMenuEntry._fixup_menuentry(raw_entry, slot_boot_id=slot_boot_id)

        assert f"echo 'Booting from {slot_boot_id} ...'" in result

        # Round-trip: iter_menuentries should parse exactly one entry back out,
        # and it should match the fixup result.
        parsed = list(iter_menuentries(result))
        assert len(parsed) == 1
        assert parsed[0] == result.strip()


# ============================================================
# _BootMenuEntry.generate_menuentry
# ============================================================


class TestBootMenuEntryGenerateMenuentry:
    @pytest.mark.parametrize(
        ("grub_cfg", "kernel_ver", "slot_boot_id"),
        [
            pytest.param(*_GRUB_CFG_KERNEL_VER, OTASlotBootID.slot_a, id="slot_a"),
            pytest.param(*_GRUB_CFG_KERNEL_VER, OTASlotBootID.slot_b, id="slot_b"),
        ],
    )
    def test_generate_menuentry(
        self, grub_cfg: str, kernel_ver: str, slot_boot_id: OTASlotBootID
    ):
        entry = _BootMenuEntry.generate_menuentry(
            grub_cfg, slot_boot_id=slot_boot_id, kernel_ver=kernel_ver
        )
        assert entry.slot_boot_id == slot_boot_id
        assert entry.kernel_ver == kernel_ver
        assert f"vmlinuz-{kernel_ver}" in entry.raw_entry
        assert f"'{slot_boot_id}'" in entry.raw_entry
        assert f"echo 'Booting from {slot_boot_id} ...'" in entry.raw_entry

    @pytest.mark.parametrize(
        "kernel_ver, slot_boot_id",
        [
            pytest.param("ota", OTASlotBootID.slot_a, id="ota_kernel_slot_a"),
            pytest.param(
                "ota.standby", OTASlotBootID.slot_b, id="ota_standby_kernel_slot_b"
            ),
        ],
    )
    def test_generate_menuentry_from_old_grub_cfg(
        self, kernel_ver: str, slot_boot_id: OTASlotBootID
    ):
        """old_grub_boot_grub.cfg has legacy ota/ota.standby kernel entries,
        used only for testing migration from old to new boot control."""
        entry = _BootMenuEntry.generate_menuentry(
            OLD_GRUB_BOOT_CFG, slot_boot_id=slot_boot_id, kernel_ver=kernel_ver
        )
        assert entry.slot_boot_id == slot_boot_id
        assert entry.kernel_ver == kernel_ver
        assert f"vmlinuz-{kernel_ver}" in entry.raw_entry
        assert f"'{slot_boot_id}'" in entry.raw_entry
        assert f"echo 'Booting from {slot_boot_id} ...'" in entry.raw_entry

    def test_raises_on_nonexistent_kernel(self):
        with pytest.raises(ValueError, match="failed to find menuentry"):
            _BootMenuEntry.generate_menuentry(
                GRUB_CFG,
                slot_boot_id=OTASlotBootID.slot_a,
                kernel_ver="99.99.0-nonexistent",
            )

    def test_raises_on_ota_managed_cfg_no_entries(self):
        """ota_managed_grub.cfg has no menuentry blocks, so generate should fail."""
        with pytest.raises(ValueError, match="failed to find menuentry"):
            _BootMenuEntry.generate_menuentry(
                OTA_MANAGED_GRUB_CFG,
                slot_boot_id=OTASlotBootID.slot_a,
                kernel_ver="6.11.0-29-generic",
            )


# ============================================================
# _GrubBootHelperFuncs._update_grub_default
# ============================================================


class TestUpdateGrubDefault:
    def test_grub_default_to_ota_managed(self):
        result = _GrubBootHelperFuncs._update_grub_default(GRUB_DEFAULT)
        assert result == EXPECTED_OTA_MANAGED_DEFAULT

    def test_empty_input_includes_all_defaults(self):
        result = _GrubBootHelperFuncs._update_grub_default("")
        for key, expected_value in GRUB_DEFAULT_OPTIONS.items():
            assert f"{key}={expected_value}" in result

    def test_preserves_non_overridden_options(self):
        result = _GrubBootHelperFuncs._update_grub_default(GRUB_DEFAULT)
        result_kvp = {}
        for line in result.splitlines():
            if line.startswith("#") or not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            result_kvp[key] = value

        # Collect non-overridden, non-blacklisted keys from the input.
        for line in GRUB_DEFAULT.splitlines():
            if line.startswith("#") or not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key in GRUB_DEFAULT_OPTIONS or key in GRUB_BLACKLIST_OPTIONS:
                continue
            assert key in result_kvp, f"{key} should be preserved"
            assert result_kvp[key] == value, f"{key} value should be unchanged"

    def test_overrides_with_ota_defaults(self):
        result = _GrubBootHelperFuncs._update_grub_default(GRUB_DEFAULT)
        for key, expected_value in GRUB_DEFAULT_OPTIONS.items():
            lines = [
                _line for _line in result.splitlines() if _line.startswith(key + "=")
            ]
            assert len(lines) == 1, f"expected exactly one {key}= line"
            assert lines[0] == f"{key}={expected_value}"

    @pytest.mark.parametrize(
        "blacklisted_option",
        [pytest.param(opt, id=opt) for opt in GRUB_BLACKLIST_OPTIONS],
    )
    def test_strips_blacklisted_options(self, blacklisted_option: str):
        result = _GrubBootHelperFuncs._update_grub_default(
            GRUB_DEFAULT_WITH_BLACKLISTED
        )
        assert blacklisted_option not in result

    def test_duplicate_keys_last_value_wins_then_overridden(self):
        result = _GrubBootHelperFuncs._update_grub_default(GRUB_DEFAULT_WITH_DUPLICATES)
        timeout_lines = [
            _line for _line in result.splitlines() if _line.startswith("GRUB_TIMEOUT=")
        ]
        assert len(timeout_lines) == 1
        assert (
            timeout_lines[0] == f"GRUB_TIMEOUT={GRUB_DEFAULT_OPTIONS['GRUB_TIMEOUT']}"
        )

    @pytest.mark.parametrize(
        "input_line",
        [
            pytest.param("# this is a comment", id="comment_line"),
            pytest.param("", id="empty_line"),
            pytest.param("no_equals_sign_here", id="missing_equals"),
        ],
    )
    def test_skips_non_option_lines(self, input_line: str):
        result = _GrubBootHelperFuncs._update_grub_default(input_line)
        for line in result.splitlines():
            if line.startswith("#"):
                continue
            if not line:
                continue
            key = line.split("=", 1)[0]
            assert key in GRUB_DEFAULT_OPTIONS


# The real-world lsblk --json output provided during development.
_LSBLK_JSON_NVME = json.dumps(
    {
        "blockdevices": [
            {
                "name": "/dev/nvme0n1",
                "fstype": None,
                "uuid": None,
                "parttype": None,
                "children": [
                    {
                        "name": "/dev/nvme0n1p1",
                        "fstype": "vfat",
                        "uuid": "CB4C-72D3",
                        "parttype": "c12a7328-f81f-11d2-ba4b-00a0c93ec93b",
                    },
                    {
                        "name": "/dev/nvme0n1p2",
                        "fstype": "ext4",
                        "uuid": "d9a87440-5dcf-4fa1-994a-e84f8a9ae9df",
                        "parttype": "0fc63daf-8483-4772-8e79-3d69d8477de4",
                    },
                    {
                        "name": "/dev/nvme0n1p3",
                        "fstype": "ext4",
                        "uuid": "5f563e84-6422-4844-aa82-f912cf8561b8",
                        "parttype": "0fc63daf-8483-4772-8e79-3d69d8477de4",
                    },
                    {
                        "name": "/dev/nvme0n1p4",
                        "fstype": "ext4",
                        "uuid": "cb7519f4-924b-4f2c-ac30-9318e31cf64e",
                        "parttype": "0fc63daf-8483-4772-8e79-3d69d8477de4",
                    },
                ],
            }
        ]
    }
)

_LSBLK_JSON_SDA = json.dumps(
    {
        "blockdevices": [
            {
                "name": "/dev/sda",
                "fstype": None,
                "uuid": None,
                "parttype": None,
                "children": [
                    {
                        "name": "/dev/sda1",
                        "fstype": "ext4",
                        "uuid": "aaaa-1111",
                        "parttype": "0fc63daf-8483-4772-8e79-3d69d8477de4",
                    },
                    {
                        "name": "/dev/sda2",
                        "fstype": "ext4",
                        "uuid": "bbbb-2222",
                        "parttype": "0fc63daf-8483-4772-8e79-3d69d8477de4",
                    },
                ],
            }
        ]
    }
)


class TestListPartitions:
    """Tests for ABPartitionDetector._list_partitions."""

    def test_parses_nvme_partitions(self, mocker):
        mock_cmdhelper = mocker.patch("otaclient.boot_control._grub_new.cmdhelper")
        mock_cmdhelper.get_parent_dev.return_value = "/dev/nvme0n1"
        mock_cmdhelper.subprocess_check_output.return_value = _LSBLK_JSON_NVME

        result = ABPartitionDetector._list_partitions("/dev/nvme0n1p3")

        assert result == {
            "1": PartitionInfo(
                dev="/dev/nvme0n1p1",
                uuid="CB4C-72D3",
                parttype="c12a7328-f81f-11d2-ba4b-00a0c93ec93b",
            ),
            "2": PartitionInfo(
                dev="/dev/nvme0n1p2",
                uuid="d9a87440-5dcf-4fa1-994a-e84f8a9ae9df",
                parttype="0fc63daf-8483-4772-8e79-3d69d8477de4",
            ),
            "3": PartitionInfo(
                dev="/dev/nvme0n1p3",
                uuid="5f563e84-6422-4844-aa82-f912cf8561b8",
                parttype="0fc63daf-8483-4772-8e79-3d69d8477de4",
            ),
            "4": PartitionInfo(
                dev="/dev/nvme0n1p4",
                uuid="cb7519f4-924b-4f2c-ac30-9318e31cf64e",
                parttype="0fc63daf-8483-4772-8e79-3d69d8477de4",
            ),
        }

    def test_parses_sda_partitions(self, mocker):
        mock_cmdhelper = mocker.patch("otaclient.boot_control._grub_new.cmdhelper")
        mock_cmdhelper.get_parent_dev.return_value = "/dev/sda"
        mock_cmdhelper.subprocess_check_output.return_value = _LSBLK_JSON_SDA

        result = ABPartitionDetector._list_partitions("/dev/sda1")

        assert len(result) == 2
        assert result["1"].dev == "/dev/sda1"
        assert result["2"].dev == "/dev/sda2"

    def test_no_block_devices_raises(self, mocker):
        mock_cmdhelper = mocker.patch("otaclient.boot_control._grub_new.cmdhelper")
        mock_cmdhelper.get_parent_dev.return_value = "/dev/sda"
        mock_cmdhelper.subprocess_check_output.return_value = json.dumps(
            {"blockdevices": []}
        )

        with pytest.raises(GrubBootControllerError, match="no block devices found"):
            ABPartitionDetector._list_partitions("/dev/sda1")

    def test_no_children_returns_empty(self, mocker):
        mock_cmdhelper = mocker.patch("otaclient.boot_control._grub_new.cmdhelper")
        lsblk_json = json.dumps(
            {
                "blockdevices": [
                    {
                        "name": "/dev/sda",
                        "fstype": None,
                        "uuid": None,
                        "parttype": None,
                    }
                ]
            }
        )
        mock_cmdhelper.get_parent_dev.return_value = "/dev/sda"
        mock_cmdhelper.subprocess_check_output.return_value = lsblk_json

        result = ABPartitionDetector._list_partitions("/dev/sda1")
        assert result == {}

    def test_invalid_json_raises(self, mocker):
        mock_cmdhelper = mocker.patch("otaclient.boot_control._grub_new.cmdhelper")
        mock_cmdhelper.get_parent_dev.return_value = "/dev/sda"
        mock_cmdhelper.subprocess_check_output.return_value = "not json"

        with pytest.raises(GrubBootControllerError, match="failed to detect"):
            ABPartitionDetector._list_partitions("/dev/sda1")


#
# ------ _GrubBootControl._generate_fstab ------
#


def _fstab_entry(
    uuid: str, mp: str, fstype: str, opts: str, dump: str, pass_: str
) -> str:
    return f"UUID={uuid}\t{mp}\t{fstype}\t{opts}\t{dump}\t{pass_}"


def _build_fstab(*entries: str, comments: tuple = ()) -> str:
    lines = list(comments) + [e + "\n" for e in entries]
    return "".join(lines)


# Reusable fstab entries — synthetic.
_ROOT_SYNTH = _fstab_entry("aaaa-1111", "/", "ext4", "errors=remount-ro", "0", "1")
_BOOT_SYNTH = _fstab_entry("bbbb-2222", "/boot", "ext4", "defaults", "0", "1")
_EFI_SYNTH = _fstab_entry("cccc-3333", "/boot/efi", "vfat", "umask=0077", "0", "1")
_ROOT_SYNTH_STANDBY = _fstab_entry(
    "old-root", "/", "ext4", "errors=remount-ro", "0", "1"
)
_DATA_SYNTH = _fstab_entry("dddd-4444", "/data", "ext4", "defaults", "0", "2")
_HOME_SYNTH = _fstab_entry("eeee-5555", "/home", "ext4", "defaults", "0", "2")
_VARLOG_SYNTH = _fstab_entry("ffff-6666", "/var/log", "ext4", "defaults", "0", "2")

# Reusable fstab entries — real-world OTA setup ECU.
_ROOT_REAL = _fstab_entry(
    "cb7519f4-924b-4f2c-ac30-9318e31cf64e", "/", "ext4", "errors=remount-ro", "0", "1"
)
_BOOT_REAL = _fstab_entry(
    "d9a87440-5dcf-4fa1-994a-e84f8a9ae9df", "/boot", "ext4", "defaults", "0", "1"
)
_EFI_REAL = _fstab_entry("CB4C-72D3", "/boot/efi", "vfat", "defaults", "0", "1")
_DATA_REAL = _fstab_entry(
    "ba7ed9ca-0188-4f66-bb01-b1ac990f2a31", "/data", "ext4", "defaults", "0", "2"
)

_SYNTH_STANDBY_FSUUID = "new-standby-uuid"
_REAL_STANDBY_FSUUID = "5f563e84-6422-4844-aa82-f912cf8561b8"


def _make_grub_ctrl(mocker, *, boot_uuid, efi_uuid):
    """Create a _GrubBootControl instance bypassing __init__."""
    ctrl = object.__new__(_GrubBootControl)
    boot_slots = mocker.MagicMock()
    boot_slots.boot_partition.uuid = boot_uuid
    if efi_uuid is not None:
        boot_slots.efi_partition.uuid = efi_uuid
    else:
        boot_slots.efi_partition = None
    ctrl.boot_slots = boot_slots
    return ctrl


@pytest.mark.parametrize(
    "boot_uuid, efi_uuid, slot_fsuuid, reference_fstab, base_fstab, expected_lines",
    [
        pytest.param(
            "bbbb-2222",
            "cccc-3333",
            _SYNTH_STANDBY_FSUUID,
            _build_fstab(
                _ROOT_SYNTH,
                _BOOT_SYNTH,
                _EFI_SYNTH,
                comments=("# /etc/fstab: static file system information.\n",),
            ),
            _build_fstab(_ROOT_SYNTH_STANDBY, _BOOT_SYNTH, _EFI_SYNTH, _DATA_SYNTH),
            [
                _fstab_entry(
                    _SYNTH_STANDBY_FSUUID, "/", "ext4", "errors=remount-ro", "0", "1"
                ),
                _BOOT_SYNTH,
                _EFI_SYNTH,
                _DATA_SYNTH,
            ],
            id="synth_with_extra_mount",
        ),
        pytest.param(
            "bbbb-2222",
            "cccc-3333",
            _SYNTH_STANDBY_FSUUID,
            _build_fstab(_ROOT_SYNTH, _BOOT_SYNTH, _EFI_SYNTH),
            _build_fstab(
                _ROOT_SYNTH_STANDBY,
                _BOOT_SYNTH,
                _EFI_SYNTH,
                _DATA_SYNTH,
                _HOME_SYNTH,
                _VARLOG_SYNTH,
            ),
            [
                _fstab_entry(
                    _SYNTH_STANDBY_FSUUID, "/", "ext4", "errors=remount-ro", "0", "1"
                ),
                _BOOT_SYNTH,
                _EFI_SYNTH,
                _DATA_SYNTH,
                _HOME_SYNTH,
                _VARLOG_SYNTH,
            ],
            id="synth_multiple_extra_mounts",
        ),
        pytest.param(
            "d9a87440-5dcf-4fa1-994a-e84f8a9ae9df",
            "CB4C-72D3",
            _REAL_STANDBY_FSUUID,
            _build_fstab(_ROOT_REAL, _BOOT_REAL, _EFI_REAL),
            _build_fstab(_ROOT_REAL, _BOOT_REAL, _EFI_REAL),
            [
                _fstab_entry(
                    _REAL_STANDBY_FSUUID, "/", "ext4", "errors=remount-ro", "0", "1"
                ),
                _BOOT_REAL,
                _EFI_REAL,
            ],
            id="real_ecu",
        ),
        pytest.param(
            "d9a87440-5dcf-4fa1-994a-e84f8a9ae9df",
            "CB4C-72D3",
            _REAL_STANDBY_FSUUID,
            _build_fstab(_ROOT_REAL, _BOOT_REAL, _EFI_REAL),
            _build_fstab(_ROOT_REAL, _BOOT_REAL, _EFI_REAL, _DATA_REAL),
            [
                _fstab_entry(
                    _REAL_STANDBY_FSUUID, "/", "ext4", "errors=remount-ro", "0", "1"
                ),
                _BOOT_REAL,
                _EFI_REAL,
                _DATA_REAL,
            ],
            id="real_ecu_with_extra_mount",
        ),
    ],
)
class TestGenerateFstab:
    """Tests for _GrubBootControl._generate_fstab."""

    def test_expected_output(
        self,
        mocker,
        boot_uuid,
        efi_uuid,
        slot_fsuuid,
        reference_fstab,
        base_fstab,
        expected_lines,
    ):
        ctrl = _make_grub_ctrl(mocker, boot_uuid=boot_uuid, efi_uuid=efi_uuid)
        result = ctrl._generate_fstab(
            base_fstab=base_fstab,
            reference_fstab=reference_fstab,
            slot_fsuuid=slot_fsuuid,
        )
        assert result.strip().splitlines() == expected_lines

    def test_trailing_newline(
        self,
        mocker,
        boot_uuid,
        efi_uuid,
        slot_fsuuid,
        reference_fstab,
        base_fstab,
        expected_lines,
    ):
        ctrl = _make_grub_ctrl(mocker, boot_uuid=boot_uuid, efi_uuid=efi_uuid)
        result = ctrl._generate_fstab(
            base_fstab=base_fstab,
            reference_fstab=reference_fstab,
            slot_fsuuid=slot_fsuuid,
        )
        assert result.endswith("\n")

    def test_no_comments_in_output(
        self,
        mocker,
        boot_uuid,
        efi_uuid,
        slot_fsuuid,
        reference_fstab,
        base_fstab,
        expected_lines,
    ):
        ctrl = _make_grub_ctrl(mocker, boot_uuid=boot_uuid, efi_uuid=efi_uuid)
        result = ctrl._generate_fstab(
            base_fstab=base_fstab,
            reference_fstab=reference_fstab,
            slot_fsuuid=slot_fsuuid,
        )
        for line in result.strip().splitlines():
            assert not line.startswith("#")

    def test_special_entries_not_duplicated(
        self,
        mocker,
        boot_uuid,
        efi_uuid,
        slot_fsuuid,
        reference_fstab,
        base_fstab,
        expected_lines,
    ):
        ctrl = _make_grub_ctrl(mocker, boot_uuid=boot_uuid, efi_uuid=efi_uuid)
        result = ctrl._generate_fstab(
            base_fstab=base_fstab,
            reference_fstab=reference_fstab,
            slot_fsuuid=slot_fsuuid,
        )
        mount_points = [line.split("\t")[1] for line in result.strip().splitlines()]
        assert mount_points.count("/") == 1
        assert mount_points.count("/boot") == 1
        assert mount_points.count("/boot/efi") == 1

    def test_boot_appears_before_efi(
        self,
        mocker,
        boot_uuid,
        efi_uuid,
        slot_fsuuid,
        reference_fstab,
        base_fstab,
        expected_lines,
    ):
        ctrl = _make_grub_ctrl(mocker, boot_uuid=boot_uuid, efi_uuid=efi_uuid)
        result = ctrl._generate_fstab(
            base_fstab=base_fstab,
            reference_fstab=reference_fstab,
            slot_fsuuid=slot_fsuuid,
        )
        mount_points = [line.split("\t")[1] for line in result.strip().splitlines()]
        assert mount_points.index("/boot") < mount_points.index("/boot/efi")


class TestGenerateFstabFallback:
    """Tests for _generate_fstab fallback paths (no reference or missing entries)."""

    @pytest.mark.parametrize(
        "boot_uuid, efi_uuid, reference_fstab, slot_fsuuid, "
        "expected_root, expected_boot, expected_efi",
        [
            pytest.param(
                "bbbb-2222",
                "cccc-3333",
                None,
                _SYNTH_STANDBY_FSUUID,
                _fstab_entry(
                    _SYNTH_STANDBY_FSUUID, "/", "ext4", "errors=remount-ro", "0", "1"
                ),
                _fstab_entry("bbbb-2222", "/boot", "ext4", "defaults", "0", "1"),
                _fstab_entry("cccc-3333", "/boot/efi", "vfat", "defaults", "0", "1"),
                id="no_reference",
            ),
            pytest.param(
                "boot-uuid-fb",
                "efi-uuid-fb",
                _build_fstab(_ROOT_SYNTH),
                _SYNTH_STANDBY_FSUUID,
                _fstab_entry(
                    _SYNTH_STANDBY_FSUUID, "/", "ext4", "errors=remount-ro", "0", "1"
                ),
                _fstab_entry("boot-uuid-fb", "/boot", "ext4", "defaults", "0", "1"),
                _fstab_entry("efi-uuid-fb", "/boot/efi", "vfat", "defaults", "0", "1"),
                id="reference_missing_boot_and_efi",
            ),
        ],
    )
    def test_fallback_entries(
        self,
        mocker,
        boot_uuid,
        efi_uuid,
        reference_fstab,
        slot_fsuuid,
        expected_root,
        expected_boot,
        expected_efi,
    ):
        ctrl = _make_grub_ctrl(mocker, boot_uuid=boot_uuid, efi_uuid=efi_uuid)
        result = ctrl._generate_fstab(
            base_fstab=_build_fstab(_ROOT_SYNTH_STANDBY),
            reference_fstab=reference_fstab,
            slot_fsuuid=slot_fsuuid,
        )
        lines = result.strip().splitlines()
        assert lines[0] == expected_root
        assert lines[1] == expected_boot
        assert lines[2] == expected_efi

    def test_no_efi_partition_omits_efi_entry(self, mocker):
        """When efi_partition is None, /boot/efi entry is not included."""
        ctrl = _make_grub_ctrl(mocker, boot_uuid="bbbb-2222", efi_uuid=None)
        result = ctrl._generate_fstab(
            base_fstab=_build_fstab(
                _ROOT_SYNTH_STANDBY,
                _BOOT_SYNTH,
                _EFI_SYNTH,
                _DATA_SYNTH,
            ),
            reference_fstab=_build_fstab(_ROOT_SYNTH, _BOOT_SYNTH, _EFI_SYNTH),
            slot_fsuuid=_SYNTH_STANDBY_FSUUID,
        )
        mount_points = [line.split("\t")[1] for line in result.strip().splitlines()]
        assert "/" in mount_points
        assert "/boot" in mount_points
        assert "/boot/efi" not in mount_points
        assert "/data" in mount_points


#
# ------ kernel/initrd hardlink at /boot root for old grub backward compat ------
#
# Both _bootstrap_setup_boot_slot_dir and setup_boot_slot_dir must:
# - place the boot files under /boot/ota-slot_<id>/ (real file)
# - expose them at the /boot/ root via a hardlink to the slot dir copy
#   (the old grub controller's bootstrap predicate checks `is_file` at
#   /boot/<vmlinuz-uname-r>).


def _patch_boot_dpath(mocker, boot_dir: Path) -> None:
    """Point boot_cfg.BOOT_DPATH at a tmp dir for the duration of a test."""
    mocker.patch(
        "otaclient.boot_control._grub_new.boot_cfg.BOOT_DPATH",
        str(boot_dir),
    )


def _make_partial_grub_ctrl(
    mocker, *, current_slot: OTASlotBootID | None = None
) -> _GrubBootControl:
    """Build a _GrubBootControl bypassing __init__ for boot slot dir tests."""
    ctrl = object.__new__(_GrubBootControl)
    boot_slots = mocker.MagicMock()
    if current_slot is not None:
        boot_slots.current_slot = current_slot
    ctrl.boot_slots = boot_slots
    return ctrl


class TestBootstrapSetupBootSlotDir:
    """Tests for _GrubBootControl._bootstrap_setup_boot_slot_dir.

    Verifies the boot files are placed in the slot dir AND hardlinked at
    the /boot/ root (same inode), so the old grub controller's bootstrap
    predicate passes without us paying for two full copies on the boot fs.
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
        """/boot/<vmlinuz> and /boot/<initrd> are hardlinks to the slot dir copy."""
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
        """Pre-existing /boot/<vmlinuz> from a prior bootstrap is replaced
        with a fresh hardlink pointing at the new slot dir copy.
        """
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
        system, case 2), the same file is hardlinked to /boot root with no
        intermediate copy.
        """
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
    """Tests for _GrubBootControl.setup_boot_slot_dir.

    Verifies kernel/initrd are copied from the slot mount point (post-OTA)
    into the slot's boot dir, and exposed at /boot/ root via hardlink.
    """

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
        """A leftover /boot/<vmlinuz> from a previous OTA must be replaced
        with a hardlink to the new slot dir copy, not retain stale content.
        """
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
        """If `vmlinuz-<ver>` in the slot mount point is itself a symlink
        (not a regular file), the loop skips it — no slot dir copy and no
        /boot root hardlink is produced.
        """
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


# ---------------------------------------------------------------------------
# Backward-compat helpers: setup-case detection and slot_in_use translation.
# These are unit-level tests for pure-function behavior. End-to-end migration /
# mirror / wipe coverage lives in
# test/integration/.../test_grub_backward_compat.py.
# ---------------------------------------------------------------------------


class TestDetectBootControlSetupCase:
    """Verify ``_detect_boot_control_setup_case`` classifies the three layouts.

    Pure function over the contents of ``boot_cfg.GRUB_CFG_FPATH``; no
    controller construction needed. We only redirect that single path to a
    tmp_path location.
    """

    @pytest.fixture
    def grub_cfg_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Redirect ``boot_cfg.GRUB_CFG_FPATH`` to a path under tmp_path."""
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
        grub_cfg_path.write_text(GRUB_CFG)
        assert _detect_boot_control_setup_case() == _GrubBootControlSetupCase.FRESH

    def test_fresh_when_grub_cfg_unreadable(
        self, grub_cfg_path: Path, mocker: pytest.MonkeyPatch
    ):
        """A grub.cfg that can't be read (e.g. permission denied) → FRESH.

        Logged-warning + FRESH is the intended fallback per
        ``_detect_boot_control_setup_case``.
        """
        # Write *something* so the symlink and exists checks pass, then
        # cause read_text to raise OSError.
        grub_cfg_path.write_text(GRUB_CFG)
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
        managed = OTAManagedCfg(raw_contents=GRUB_CFG.strip(), grub_version="2.12")
        grub_cfg_path.write_text(managed.export())
        assert (
            _detect_boot_control_setup_case() == _GrubBootControlSetupCase.ALREADY_NEW
        )


class TestTranslateSlotInUseOldToNew:
    """Verify ``_translate_slot_in_use_old_to_new`` covers all paths.

    Pure logic over ``boot_slots.old_slot_id_mapping``; no controller
    construction needed. We bypass ``__init__`` via ``object.__new__`` and
    attach a minimal ``boot_slots`` mock.
    """

    @staticmethod
    def _make_ctrl(mocker, *, current_slot: OTASlotBootID = OTASlotBootID.slot_a):
        """Build a `_GrubBootControl` with just the mapping/current_slot needed."""
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
        """Returned value is a ``StrEnum`` instance and equals the on-disk
        new-format string ``"ota-slot_<a/b>"`` via str equality."""
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
        # Falls back to current_slot (slot_b in this scenario), not to slot_a
        assert (
            ctrl._translate_slot_in_use_old_to_new("ota-partition.sda3")
            == OTASlotBootID.slot_b
        )
        assert any("unrecognised" in rec.message for rec in caplog.records)

    def test_translate_empty_string_falls_back(self, mocker, caplog):
        """Empty input → fallback. (Migrate helper guards against empty
        before calling, but the helper itself handles it gracefully.)"""
        ctrl = self._make_ctrl(mocker, current_slot=OTASlotBootID.slot_a)
        assert ctrl._translate_slot_in_use_old_to_new("") == OTASlotBootID.slot_a
        assert any("unrecognised" in rec.message for rec in caplog.records)
