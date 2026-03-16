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

import pytest

from otaclient.boot_control._grub_common import OTA_MANAGED_CFG_HEADER, OTASlotBootID
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
    _BootMenuEntry,
    _GrubBootHelperFuncs,
    iter_menuentries,
)

from .conftest import (
    GRUB_CFG,
    GRUB_DEFAULT,
    GRUB_DEFAULT_WITH_BLACKLISTED,
    GRUB_DEFAULT_WITH_DUPLICATES,
    OLD_GRUB_BOOT_CFG,
    OTA_MANAGED_GRUB_CFG,
)

# Shared test data: (grub_cfg, kernel_ver) for the standard grub.cfg test file.
_GRUB_CFG_KERNEL_VER = (GRUB_CFG, "5.19.0-50-generic")


# ============================================================
# Regex patterns
# ============================================================


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
        """The skip logic checks `_linux_dir_ma.group()` (the matched `linux <fpath>`),
        so "recovery" must appear in the fpath for the skip to trigger.

        Using `/recovery/vmlinuz-...` ensures LINUX_VERSION_PA still extracts the
        correct kernel version, but the recovery check fires on the full match.
        """
        recovery = (
            "menuentry 'Ubuntu (recovery)' $menuentry_id_option 'recovery' {\n"
            "\tlinux\t/recovery/vmlinuz-5.19.0-50-generic root=UUID=abc ro\n"
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


# ============================================================
# _BootMenuEntry._fixup_fpath
# ============================================================


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
        """ota_mamanged_grub.cfg has no menuentry blocks, so generate should fail."""
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
    def test_starts_with_ota_managed_header(self):
        result = _GrubBootHelperFuncs._update_grub_default("")
        assert result.startswith(OTA_MANAGED_CFG_HEADER)

    def test_ends_with_trailing_newline(self):
        result = _GrubBootHelperFuncs._update_grub_default("")
        assert result.endswith("\n")

    @pytest.mark.parametrize(
        "key, expected_value",
        [pytest.param(k, v, id=k) for k, v in GRUB_DEFAULT_OPTIONS.items()],
    )
    def test_empty_input_includes_all_defaults(self, key: str, expected_value: str):
        result = _GrubBootHelperFuncs._update_grub_default("")
        assert f"{key}={expected_value}" in result

    @pytest.mark.parametrize(
        "input_str, preserved_key, preserved_value",
        [
            pytest.param(
                GRUB_DEFAULT,
                "GRUB_DISTRIBUTOR",
                "`( . /etc/os-release; echo ${NAME:-Ubuntu} ) 2>/dev/null || echo Ubuntu`",
                id="preserves_distributor",
            ),
            pytest.param(
                GRUB_DEFAULT,
                "GRUB_CMDLINE_LINUX_DEFAULT",
                '"quiet splash"',
                id="preserves_cmdline_default",
            ),
            pytest.param(
                GRUB_DEFAULT,
                "GRUB_CMDLINE_LINUX",
                '""',
                id="preserves_cmdline_linux",
            ),
        ],
    )
    def test_preserves_non_overridden_options(
        self, input_str: str, preserved_key: str, preserved_value: str
    ):
        result = _GrubBootHelperFuncs._update_grub_default(input_str)
        assert f"{preserved_key}={preserved_value}" in result

    @pytest.mark.parametrize(
        "key, expected_value",
        [
            pytest.param(
                "GRUB_TIMEOUT",
                GRUB_DEFAULT_OPTIONS["GRUB_TIMEOUT"],
                id="overrides_timeout",
            ),
            pytest.param(
                "GRUB_TIMEOUT_STYLE",
                GRUB_DEFAULT_OPTIONS["GRUB_TIMEOUT_STYLE"],
                id="overrides_timeout_style",
            ),
            pytest.param(
                "GRUB_DEFAULT",
                GRUB_DEFAULT_OPTIONS["GRUB_DEFAULT"],
                id="overrides_default",
            ),
        ],
    )
    def test_overrides_with_ota_defaults(self, key: str, expected_value: str):
        result = _GrubBootHelperFuncs._update_grub_default(GRUB_DEFAULT)
        lines = [_line for _line in result.splitlines() if _line.startswith(key + "=")]
        assert len(lines) == 1
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
