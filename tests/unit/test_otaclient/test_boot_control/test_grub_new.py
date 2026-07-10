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

import pytest

from otaclient.boot_control._grub_common import (
    GrubBootControllerError,
    OTASlotBootID,
    PartitionInfo,
)
from otaclient.boot_control._grub_new import (
    DEV_PATH_PA,
    GRUB_BLACKLIST_OPTIONS,
    GRUB_DEFAULT_OPTIONS,
    INITRD_PA_MULTILINE,
    LINUX_PA_MULTILINE,
    LINUX_VERSION_PA,
    MENUENTRY_HEAD_PA,
    MENUENTRY_ID_PA,
    MENUENTRY_TITLE_PA,
    ABPartitionDetector,
    _BootMenuEntry,
    _GrubBootControl,
    _GrubBootHelperFuncs,
    _release_tuple,
    iter_menuentries,
)
from otaclient.boot_control.configs import grub_new_cfg as boot_cfg

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


#
# ------------ iter_menuentries ------------ #
#


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


#
# ------------ _BootMenuEntry._find_menuentry ------------ #
#


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


#
# ------------ _BootMenuEntry._fixup_menuentry ------------ #
#


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


#
# ------------ _BootMenuEntry.generate_menuentry ------------ #
#


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


#
# ------------ _GrubBootHelperFuncs._update_grub_default ------------ #
#


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
# ------------ _GrubBootControl._generate_fstab ------------ #
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
# ------------ _GrubBootControl._rewrite_slot_aware_partlabel ------------ #
#


class TestRewriteSlotAwarePartlabel:
    @pytest.mark.parametrize(
        "dev, target_suffix, expected",
        [
            # bulk / greengrass are slot-aware — rewrite regardless of direction.
            ("/dev/disk/by-partlabel/bulk_a", "b", "/dev/disk/by-partlabel/bulk_b"),
            ("/dev/disk/by-partlabel/bulk_b", "a", "/dev/disk/by-partlabel/bulk_a"),
            (
                "/dev/disk/by-partlabel/greengrass_a",
                "b",
                "/dev/disk/by-partlabel/greengrass_b",
            ),
            (
                "/dev/disk/by-partlabel/greengrass_b",
                "a",
                "/dev/disk/by-partlabel/greengrass_a",
            ),
            # Same-suffix retarget is idempotent (rollback re-runs).
            ("/dev/disk/by-partlabel/bulk_a", "a", "/dev/disk/by-partlabel/bulk_a"),
        ],
    )
    def test_rewrites_slot_aware(self, dev: str, target_suffix: str, expected: str):
        assert (
            _GrubBootControl._rewrite_slot_aware_partlabel(dev, target_suffix)
            == expected
        )

    @pytest.mark.parametrize(
        "dev",
        [
            # rootfs / rw_overlay are NOT in this design's fstab and are not
            # in SLOT_AWARE_PARTLABEL_PREFIXES — pass through even with _a/_b.
            "/dev/disk/by-partlabel/rootfs_a",
            "/dev/disk/by-partlabel/rw_overlay_b",
            # Non-slot-aware PARTLABELs pass through.
            "/dev/disk/by-partlabel/EFI",
            "/dev/disk/by-partlabel/boot",
            # UUID / raw dev / tmpfs / bind sources — pass through untouched.
            "UUID=cb7519f4-924b-4f2c-ac30-9318e31cf64e",
            "/dev/nvme0n1p3",
            "tmpfs",
            "/data/bulk/var-log",
            # Prefix matches "bulk" but suffix isn't _a/_b — pass through.
            "/dev/disk/by-partlabel/bulk_data",
        ],
    )
    def test_passes_through_non_slot_aware(self, dev: str):
        assert _GrubBootControl._rewrite_slot_aware_partlabel(dev, "b") == dev
        assert _GrubBootControl._rewrite_slot_aware_partlabel(dev, "a") == dev


#
# ------------ _GrubBootControl._generate_fstab: slot-aware rewrite ------------ #
#


def _make_grub_ctrl_with_standby(mocker, *, standby_slot: OTASlotBootID):
    """_make_grub_ctrl variant that also sets boot_slots.standby_slot."""
    ctrl = _make_grub_ctrl(mocker, boot_uuid="bbbb-2222", efi_uuid="cccc-3333")
    ctrl.boot_slots.standby_slot = standby_slot
    return ctrl


def _partlabel_entry(label: str, mp: str, fstype: str = "ext4") -> str:
    return f"/dev/disk/by-partlabel/{label}\t{mp}\t{fstype}\trw,noatime,nosuid,nodev\t0\t2"


class TestGenerateFstabSlotAwareRewrite:
    """Standby fstab is baked at image build with one slot suffix (typically _a).
    _generate_fstab retargets bulk / greengrass to the standby slot at cutover
    so writes go to this slot's own copy (OTA_PARTITION_DESIGN_EN.md §8)."""

    def test_a_to_b_rewrites_bulk_and_greengrass(self, mocker):
        """OTA A→B: standby=B. Image-baked _a entries → _b."""
        ctrl = _make_grub_ctrl_with_standby(mocker, standby_slot=OTASlotBootID.slot_b)
        base = _build_fstab(
            _ROOT_SYNTH_STANDBY,
            _BOOT_SYNTH,
            _EFI_SYNTH,
            _partlabel_entry("bulk_a", "/data/bulk"),
            _partlabel_entry("greengrass_a", "/greengrass"),
        )
        ref = _build_fstab(_ROOT_SYNTH, _BOOT_SYNTH, _EFI_SYNTH)

        result = ctrl._generate_fstab(
            base_fstab=base, reference_fstab=ref, slot_fsuuid=_SYNTH_STANDBY_FSUUID
        )
        lines = result.strip().splitlines()
        assert _partlabel_entry("bulk_b", "/data/bulk") in lines
        assert _partlabel_entry("greengrass_b", "/greengrass") in lines
        # No _a entries should survive.
        assert not any("bulk_a" in line or "greengrass_a" in line for line in lines)

    def test_b_to_a_rewrites_bulk_and_greengrass(self, mocker):
        """OTA B→A (or rollback+cycle): standby=A. _b entries → _a."""
        ctrl = _make_grub_ctrl_with_standby(mocker, standby_slot=OTASlotBootID.slot_a)
        base = _build_fstab(
            _ROOT_SYNTH_STANDBY,
            _BOOT_SYNTH,
            _EFI_SYNTH,
            _partlabel_entry("bulk_b", "/data/bulk"),
            _partlabel_entry("greengrass_b", "/greengrass"),
        )
        ref = _build_fstab(_ROOT_SYNTH, _BOOT_SYNTH, _EFI_SYNTH)

        result = ctrl._generate_fstab(
            base_fstab=base, reference_fstab=ref, slot_fsuuid=_SYNTH_STANDBY_FSUUID
        )
        lines = result.strip().splitlines()
        assert _partlabel_entry("bulk_a", "/data/bulk") in lines
        assert _partlabel_entry("greengrass_a", "/greengrass") in lines
        assert not any("bulk_b" in line or "greengrass_b" in line for line in lines)

    def test_bind_source_paths_are_not_rewritten(self, mocker):
        """Bind sources (`/data/bulk/var-log` etc.) live in the file_system
        column too, but they aren't by-partlabel devices — pass through."""
        ctrl = _make_grub_ctrl_with_standby(mocker, standby_slot=OTASlotBootID.slot_b)
        base = _build_fstab(
            _ROOT_SYNTH_STANDBY,
            _BOOT_SYNTH,
            _EFI_SYNTH,
            _partlabel_entry("bulk_a", "/data/bulk"),
            "/data/bulk/var-log\t/var/log\tnone\tbind,nofail\t0\t0",
            "/data/bulk/rosbag\t/mnt/ROSBAG\tnone\tbind,nofail\t0\t0",
        )
        ref = _build_fstab(_ROOT_SYNTH, _BOOT_SYNTH, _EFI_SYNTH)

        result = ctrl._generate_fstab(
            base_fstab=base, reference_fstab=ref, slot_fsuuid=_SYNTH_STANDBY_FSUUID
        )
        lines = result.strip().splitlines()
        # bulk was retargeted; binds are untouched.
        assert _partlabel_entry("bulk_b", "/data/bulk") in lines
        assert (
            "/data/bulk/var-log\t/var/log\tnone\tbind,nofail\t0\t0" in lines
        )
        assert (
            "/data/bulk/rosbag\t/mnt/ROSBAG\tnone\tbind,nofail\t0\t0" in lines
        )

    def test_non_slot_aware_partlabels_pass_through(self, mocker):
        """PARTLABELs outside SLOT_AWARE_PARTLABEL_PREFIXES aren't touched."""
        ctrl = _make_grub_ctrl_with_standby(mocker, standby_slot=OTASlotBootID.slot_b)
        # `custom_a` uses the _a/_b pattern but isn't a known slot-aware prefix
        # (only bulk/greengrass are).
        custom_entry = _partlabel_entry("custom_a", "/mnt/custom")
        base = _build_fstab(
            _ROOT_SYNTH_STANDBY,
            _BOOT_SYNTH,
            _EFI_SYNTH,
            custom_entry,
        )
        ref = _build_fstab(_ROOT_SYNTH, _BOOT_SYNTH, _EFI_SYNTH)

        result = ctrl._generate_fstab(
            base_fstab=base, reference_fstab=ref, slot_fsuuid=_SYNTH_STANDBY_FSUUID
        )
        assert custom_entry in result.strip().splitlines()


#
# ------------ _GrubBootControl._ensure_legacy_compat_for_current_slot ------------ #
#

_ENSURE_KERNEL_FNAME = "vmlinuz-6.11.0-29-generic"
_ENSURE_INITRD_FNAME = "initrd.img-6.11.0-29-generic"


def _make_ensure_ctrl(mocker, tmp_path):
    """Build a partial _GrubBootControl with `boot_slots` set and the two
    path methods that ``_ensure_legacy_compat_for_current_slot`` and
    ``_mirror_legacy_compat_for_slot`` reach through (``get_boot_slot_dir``
    and ``_legacy_compat_dir_for_slot``) redirected to ``tmp_path``.

    Active slot is fixed to ``slot_a`` for these tests; legacy folder is
    ``ota-partition.sda3``; new slot folder is ``ota-slot_a``.
    """
    from pathlib import Path

    ctrl = object.__new__(_GrubBootControl)
    boot_slots = mocker.MagicMock()
    boot_slots.current_slot = OTASlotBootID.slot_a
    boot_slots.old_slot_id_mapping = {
        OTASlotBootID.slot_a: "ota-partition.sda3",
        OTASlotBootID.slot_b: "ota-partition.sda4",
    }
    ctrl.boot_slots = boot_slots

    def _legacy(slot_id):
        return Path(tmp_path) / boot_slots.old_slot_id_mapping[slot_id]

    def _new_slot(slot_id):
        return Path(tmp_path) / str(slot_id)

    ctrl._legacy_compat_dir_for_slot = _legacy
    ctrl.get_boot_slot_dir = _new_slot
    return ctrl


class TestEnsureLegacyCompatForCurrentSlot:
    """Ensure-on-startup: mirror the legacy compat folder for the active slot
    only if it doesn't already exist.

    Invoked from ``GrubBootController.__init__`` after
    ``OTAStatusFilesControl`` initialises, so that any status files freshly
    written there land in the mirror as hardlinks.
    """

    def test_skip_when_legacy_folder_exists(self, mocker, tmp_path):
        """If ``/boot/ota-partition.sda<active_pid>/`` already exists (MIGRATE_FROM_OLD
        preserved it, or a previous startup mirrored it), the ensure step
        does NOT touch its contents.
        """
        ctrl = _make_ensure_ctrl(mocker, tmp_path)
        legacy = tmp_path / "ota-partition.sda3"
        legacy.mkdir()
        # Pre-existing sentinel that a force-mirror WOULD prune.
        sentinel = legacy / "preserved-from-old-grub"
        sentinel.write_text("untouched")

        # Note: deliberately NO ``ota-slot_a/`` source dir — if the ensure
        # step tried to mirror, the call would raise. The skip path is what
        # prevents that, and is the contract under test here.
        ctrl._ensure_legacy_compat_for_current_slot()

        assert sentinel.is_file()
        assert sentinel.read_text() == "untouched"

    def test_mirrors_kernel_and_initrd_when_legacy_missing(self, mocker, tmp_path):
        """If the legacy folder is missing, the ensure step mirrors from
        ``/boot/ota-slot_<active>/`` to populate it with hardlinked
        kernel/initrd."""
        ctrl = _make_ensure_ctrl(mocker, tmp_path)
        slot_a = tmp_path / "ota-slot_a"
        slot_a.mkdir()
        (slot_a / _ENSURE_KERNEL_FNAME).write_text("kernel-bytes")
        (slot_a / _ENSURE_INITRD_FNAME).write_text("initrd-bytes")

        legacy = tmp_path / "ota-partition.sda3"
        assert not legacy.exists()

        ctrl._ensure_legacy_compat_for_current_slot()

        assert legacy.is_dir(), "legacy folder must be created"
        for fname in (_ENSURE_KERNEL_FNAME, _ENSURE_INITRD_FNAME):
            src = slot_a / fname
            dst = legacy / fname
            assert dst.is_file() and not dst.is_symlink(), (
                f"{fname} in legacy must be a real file (hardlink), not a symlink"
            )
            assert src.stat().st_ino == dst.stat().st_ino, (
                f"{fname} must be hardlinked between new slot dir and legacy"
            )

    def test_mirrors_status_files_when_present_in_source(self, mocker, tmp_path):
        """The ensure step runs AFTER ``OTAStatusFilesControl`` initialises,
        so whatever status files it just wrote into ``/boot/ota-slot_<active>/``
        are captured in the mirror.

        ``status`` / ``version`` are byte-identical between old and new grub
        control, so they are hardlinked. ``slot_in_use`` differs in format
        (``"ota-slot_a"`` new vs. ``"sda3"`` old), so the mirror translates
        new→old and writes it atomically — it is NOT hardlinked.
        """
        ctrl = _make_ensure_ctrl(mocker, tmp_path)
        slot_a = tmp_path / "ota-slot_a"
        slot_a.mkdir()
        (slot_a / _ENSURE_KERNEL_FNAME).write_text("kernel-bytes")
        (slot_a / _ENSURE_INITRD_FNAME).write_text("initrd-bytes")
        # Files that OTAStatusFilesControl would have just written.
        (slot_a / "status").write_text("SUCCESS")
        (slot_a / "slot_in_use").write_text("ota-slot_a")
        (slot_a / "version").write_text("2.0.0")

        ctrl._ensure_legacy_compat_for_current_slot()

        legacy = tmp_path / "ota-partition.sda3"
        # status / version: hardlinked (format-compatible).
        for fname in ("status", "version"):
            src = slot_a / fname
            dst = legacy / fname
            assert dst.is_file() and not dst.is_symlink()
            assert dst.read_text() == src.read_text()
            assert src.stat().st_ino == dst.stat().st_ino, (
                f"{fname} must be hardlinked into legacy"
            )
        # slot_in_use: translated new→old, NOT hardlinked.
        src_slot_in_use = slot_a / "slot_in_use"
        dst_slot_in_use = legacy / "slot_in_use"
        assert dst_slot_in_use.is_file() and not dst_slot_in_use.is_symlink()
        assert dst_slot_in_use.read_text() == "sda3", (
            "slot_in_use must be translated from new format (ota-slot_a) to "
            "old format (sda3) so a Group B old controller reads it correctly"
        )
        assert src_slot_in_use.stat().st_ino != dst_slot_in_use.stat().st_ino, (
            "slot_in_use must NOT be hardlinked — the on-disk bytes differ "
            "between old and new format"
        )

    def test_mirrors_slot_in_use_skipped_when_source_empty(self, mocker, tmp_path):
        """If the source ``slot_in_use`` is missing or empty, the mirror does
        not write a destination entry (let any old grub controller seed its
        own default via its missing-file fallback)."""
        ctrl = _make_ensure_ctrl(mocker, tmp_path)
        slot_a = tmp_path / "ota-slot_a"
        slot_a.mkdir()
        (slot_a / _ENSURE_KERNEL_FNAME).write_text("kernel-bytes")
        (slot_a / "slot_in_use").write_text("   ")  # whitespace-only

        ctrl._ensure_legacy_compat_for_current_slot()

        legacy = tmp_path / "ota-partition.sda3"
        assert not (legacy / "slot_in_use").exists(), (
            "empty source slot_in_use must not produce a destination entry"
        )

    def test_mirrors_slot_in_use_unknown_value_falls_back(self, mocker, tmp_path):
        """An unrecognised new-format ``slot_in_use`` value falls back to the
        current slot in old format (slot_a → ``sda3``) per
        :meth:`OTASlotBootID.translate_slot_in_use_new_to_old`."""
        ctrl = _make_ensure_ctrl(mocker, tmp_path)
        slot_a = tmp_path / "ota-slot_a"
        slot_a.mkdir()
        (slot_a / _ENSURE_KERNEL_FNAME).write_text("kernel-bytes")
        (slot_a / "slot_in_use").write_text("garbage-not-a-slot-id")

        ctrl._ensure_legacy_compat_for_current_slot()

        legacy = tmp_path / "ota-partition.sda3"
        assert (legacy / "slot_in_use").read_text() == "sda3", (
            "unrecognised value must translate to the current slot's old format"
        )

    def test_subdirs_in_source_are_skipped(self, mocker, tmp_path):
        """The ``grub/`` placeholder subdir under ``/boot/ota-slot_<active>/``
        is not mirrored (the mirror skips real subdirectories — only the name
        is recorded so the prune step doesn't kill a same-named legacy
        entry)."""
        ctrl = _make_ensure_ctrl(mocker, tmp_path)
        slot_a = tmp_path / "ota-slot_a"
        slot_a.mkdir()
        (slot_a / "grub").mkdir()
        (slot_a / _ENSURE_KERNEL_FNAME).write_text("kernel-bytes")

        ctrl._ensure_legacy_compat_for_current_slot()

        legacy = tmp_path / "ota-partition.sda3"
        assert (legacy / _ENSURE_KERNEL_FNAME).is_file()
        assert not (legacy / "grub").exists(), (
            "the grub/ placeholder subdir under the source must not be "
            "mirrored into legacy"
        )

    def test_symlinks_in_source_are_mirrored_as_symlinks(self, mocker, tmp_path):
        """A symlink in the source slot dir is recreated as a symlink (with
        the same target string) at the legacy destination."""
        ctrl = _make_ensure_ctrl(mocker, tmp_path)
        slot_a = tmp_path / "ota-slot_a"
        slot_a.mkdir()
        (slot_a / _ENSURE_KERNEL_FNAME).write_text("kernel-bytes")
        (slot_a / "vmlinuz-current").symlink_to(_ENSURE_KERNEL_FNAME)

        ctrl._ensure_legacy_compat_for_current_slot()

        legacy = tmp_path / "ota-partition.sda3"
        dst_link = legacy / "vmlinuz-current"
        assert dst_link.is_symlink(), "symlink must be recreated as a symlink"
        import os

        assert os.readlink(dst_link) == _ENSURE_KERNEL_FNAME, (
            "symlink target string must match the source's"
        )


#
# ------------ _GrubBootControl._disable_uefi_firmware_grub_hook ------------ #
#

# Stock-ish body for the regression baseline; not the real grub-uefi content,
# just plausibly executable shell.
_STOCK_UEFI_HOOK_BODY = "#!/bin/sh\necho 'UEFI Firmware Settings menuentry'\n"
_EXPECTED_UEFI_STUB = boot_cfg.UEFI_FIRMWARE_GRUB_HOOK_DISABLED


def _stage_uefi_hook_dir(slot_mp):
    """Create `<slot_mp>/etc/grub.d` and return the path where the
    `30_uefi-firmware` hook should live."""
    hook_dir = slot_mp / "etc" / "grub.d"
    hook_dir.mkdir(parents=True, exist_ok=True)
    return hook_dir / boot_cfg.UEFI_FIRMWARE_GRUB_HOOK_FNAME


class TestDisableUefiFirmwareGrubHook:
    """Tests for _GrubBootControl._disable_uefi_firmware_grub_hook.

    The helper neutralizes /etc/grub.d/30_uefi-firmware on the standby
    slot so grub-mkconfig does not inject the "UEFI Firmware Settings"
    menuentry into the OTA-managed grub.cfg. It removes whatever lives
    at the path (regular file or symlink) and writes a fresh
    non-executable stub. If the path is genuinely absent (not even a
    symlink), the helper short-circuits and does nothing.
    """

    def test_replaces_regular_file_with_stub(self, tmp_path):
        ctrl = object.__new__(_GrubBootControl)
        hook_fpath = _stage_uefi_hook_dir(tmp_path)
        hook_fpath.write_text(_STOCK_UEFI_HOOK_BODY)
        hook_fpath.chmod(0o755)

        ctrl._disable_uefi_firmware_grub_hook(tmp_path)

        assert hook_fpath.is_file() and not hook_fpath.is_symlink()
        assert hook_fpath.read_text() == _EXPECTED_UEFI_STUB
        mode = hook_fpath.stat().st_mode
        assert mode & 0o111 == 0, (
            f"execute bit must be cleared on the stub, got {oct(mode)}"
        )
        assert mode & 0o777 == 0o640, (
            f"expected exact mode 0o640 on the stub, got {oct(mode)}"
        )

    def test_replaces_symlink_with_real_file(self, tmp_path):
        """A symlink at the hook path is replaced by a regular stub.
        The original symlink target file is left untouched — the helper
        unlinks the link, never follows it."""
        ctrl = object.__new__(_GrubBootControl)
        hook_fpath = _stage_uefi_hook_dir(tmp_path)
        # Symlink target lives elsewhere inside slot_mp with a sentinel.
        target = tmp_path / "usr" / "lib" / "grub" / "30_uefi-firmware"
        target.parent.mkdir(parents=True)
        target.write_text("SENTINEL_TARGET_BODY")
        target.chmod(0o755)
        hook_fpath.symlink_to(target)

        ctrl._disable_uefi_firmware_grub_hook(tmp_path)

        # Hook path is now a regular file, not a symlink, and holds the stub.
        assert hook_fpath.is_file()
        assert not hook_fpath.is_symlink()
        assert hook_fpath.read_text() == _EXPECTED_UEFI_STUB
        assert hook_fpath.stat().st_mode & 0o777 == 0o640
        # The original symlink target file must be untouched.
        assert target.read_text() == "SENTINEL_TARGET_BODY", (
            "the symlink target file must NOT be modified — the helper "
            "must unlink the symlink, not follow it"
        )
        assert target.stat().st_mode & 0o111 != 0, (
            "the symlink target file's execute bit must NOT be cleared "
            "— only the hook path itself is modified"
        )

    def test_skipped_when_hook_absent(self, tmp_path):
        """If neither the file nor a symlink exists at the hook path,
        the helper short-circuits and does NOT create a stub."""
        ctrl = object.__new__(_GrubBootControl)
        hook_fpath = _stage_uefi_hook_dir(tmp_path)
        assert not hook_fpath.exists() and not hook_fpath.is_symlink()

        ctrl._disable_uefi_firmware_grub_hook(tmp_path)

        # Still absent — must not be created from thin air.
        assert not hook_fpath.exists()

    def test_replaces_broken_symlink_with_real_file(self, tmp_path):
        """A broken symlink (target does not exist) at the hook path
        still triggers the replace path — `is_symlink()` returns True
        even when `exists()` returns False.

        Pinned as its own test because a future refactor to a bare
        `exists()` predicate would silently change behaviour, and the
        previous regular-file/valid-symlink tests would still pass.
        """
        ctrl = object.__new__(_GrubBootControl)
        hook_fpath = _stage_uefi_hook_dir(tmp_path)
        hook_fpath.symlink_to(tmp_path / "does-not-exist")
        assert hook_fpath.is_symlink() and not hook_fpath.exists()

        ctrl._disable_uefi_firmware_grub_hook(tmp_path)

        assert hook_fpath.is_file() and not hook_fpath.is_symlink()
        assert hook_fpath.read_text() == _EXPECTED_UEFI_STUB
        assert hook_fpath.stat().st_mode & 0o777 == 0o640

    def test_idempotent_on_regular_file(self, tmp_path):
        """Subsequent OTAs must not corrupt the stub — calling the
        helper twice yields the same final body and mode."""
        ctrl = object.__new__(_GrubBootControl)
        hook_fpath = _stage_uefi_hook_dir(tmp_path)
        hook_fpath.write_text(_STOCK_UEFI_HOOK_BODY)
        hook_fpath.chmod(0o755)

        ctrl._disable_uefi_firmware_grub_hook(tmp_path)
        ctrl._disable_uefi_firmware_grub_hook(tmp_path)

        assert hook_fpath.read_text() == _EXPECTED_UEFI_STUB
        assert hook_fpath.stat().st_mode & 0o777 == 0o640


class TestSetupSlotRootfsWiresUefiHookDisable:
    """Wiring test: setup_slot_rootfs_for_ota_boot must invoke
    _disable_uefi_firmware_grub_hook against the same slot_mp. Without
    this, the helper could be perfectly correct yet never run in
    production.
    """

    def test_called_from_setup_slot_rootfs_for_ota_boot(self, mocker, tmp_path):
        ctrl = object.__new__(_GrubBootControl)

        # Stage the on-disk paths that the other steps in
        # setup_slot_rootfs_for_ota_boot need to read/write.
        (tmp_path / "etc").mkdir()
        (tmp_path / "etc" / "fstab").write_text("")
        (tmp_path / "etc" / "default").mkdir()
        (tmp_path / "etc" / "default" / "grub").write_text("")
        (tmp_path / "etc" / "grub.d").mkdir()

        # Stub out the fstab generator and the grub-default updater —
        # we only care that the UEFI hook disable helper is invoked.
        mocker.patch.object(_GrubBootControl, "_generate_fstab", return_value="")
        mocker.patch.object(_GrubBootControl, "_update_grub_default", return_value="")
        disable_spy = mocker.patch.object(
            _GrubBootControl, "_disable_uefi_firmware_grub_hook"
        )

        ctrl.setup_slot_rootfs_for_ota_boot(
            slot_fsuuid="fake-uuid",
            slot_mp=tmp_path,
            reference_fstab=None,
        )

        disable_spy.assert_called_once_with(tmp_path)


#
# ------------ _release_tuple ------------ #
#


class TestReleaseTuple:
    @pytest.mark.parametrize(
        "version_str, expected",
        [
            pytest.param("3.14.1", (3, 14, 1), id="plain_release"),
            pytest.param("3.14.0", (3, 14, 0), id="plain_release_lower"),
            pytest.param("3.14.1rc1", (3, 14, 1), id="rc_suffix"),
            pytest.param("3.14.1.dev3", (3, 14, 1), id="dev_suffix"),
            pytest.param(
                "3.14.1rc1.dev3+g649b8182a.d20260520",
                (3, 14, 1),
                id="full_pep440_with_local",
            ),
            pytest.param(
                "3.14.0rc4.dev19+g649b8182a.d20260520",
                (3, 14, 0),
                id="full_pep440_below_min",
            ),
            pytest.param("3.14.1.post1", (3, 14, 1), id="post_suffix"),
            pytest.param("3.14", (3, 14), id="two_segment_release"),
        ],
    )
    def test_parses_pep440(self, version_str: str, expected: tuple[int, ...]):
        assert _release_tuple(version_str) == expected

    @pytest.mark.parametrize(
        "version_str",
        [
            pytest.param("not-a-version", id="garbage_string"),
            pytest.param("", id="empty"),
            pytest.param("v3.14.1+", id="trailing_plus"),
            pytest.param("3.14.x", id="non_numeric_segment"),
        ],
    )
    def test_returns_none_on_invalid(self, version_str: str):
        assert _release_tuple(version_str) is None


def _guard__release_tuple(_in):
    if _res := _release_tuple(_in):
        return _res
    raise ValueError("invalid version string")


class TestReleaseTupleOrdering:
    """Test comparing version strings."""

    @pytest.mark.parametrize(
        "lower, higher",
        [
            # Numeric, NOT lexicographic — string compare would invert these.
            pytest.param("3.14.9", "3.14.10", id="patch_double_digit"),
            pytest.param("3.9.0", "3.10.0", id="minor_double_digit"),
            # Two-segment vs three-segment: `Version("3.14").release` is
            # (3, 14), not (3, 14, 0), so Python tuple ordering treats it as
            # strictly less than ANY three-segment release with the same
            # leading components — including (3, 14, 0). Production gating
            # uses `>= min_release`, so a recorded "3.14" cfg against a
            # "3.14.0" (or higher) minimum triggers regeneration. Pin both
            # forms so a future tweak to `_release_tuple` (e.g. zero-padding
            # to a fixed length) is a deliberate spec decision, not an
            # accident.
            pytest.param("3.14", "3.14.0", id="two_segment_vs_three_segment_zero"),
            pytest.param("3.14", "3.14.1", id="two_segment_vs_three_segment_nonzero"),
        ],
    )
    def test_lower_compares_less_than_higher(self, lower: str, higher: str):
        assert _guard__release_tuple(lower) < _guard__release_tuple(higher)

    def test_configured_min_is_parseable(self):
        """Canary on the live `GRUB_CFG_MIN_REQUIRED_OTACLIENT_VERSION`."""
        _guard__release_tuple(boot_cfg.GRUB_CFG_MIN_REQUIRED_OTACLIENT_VERSION)


# Setup-case detection (`_detect_boot_control_setup_case`) and the
# `_GrubBootControl.__init__` dispatch contract are tested as integration
# tests in test_grub_backward_compat.py, alongside the rest of the
# boot-control orchestration tests.
