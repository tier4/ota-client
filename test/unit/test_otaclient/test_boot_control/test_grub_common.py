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

import hashlib
from pathlib import Path

import pytest

from otaclient.boot_control._grub_common import (
    OTAManagedCfg,
    OTASlotBootID,
    PartitionInfo,
    SlotInfo,
    read_fstab_dict,
)

TEST_DATA_DIR = Path(__file__).parent / "data"
OTA_MANAGED_GRUB_CFG = (TEST_DATA_DIR / "ota_mamanged_grub.cfg").read_text()

BOOT_MENU_ENTRY = """\
menuentry 'Ubuntu' --class ubuntu --class gnu-linux --class gnu --class os $menuentry_id_option 'gnulinux-simple-186d206e-73e7-401c-9d8a-fad4390922f2' {
	recordfail
	load_video
	gfxmode $linux_gfx_mode
	insmod gzio
	if [ x$grub_platform = xxen ]; then insmod xzio; insmod lzopio; fi
	insmod part_gpt
	insmod ext2
	search --no-floppy --fs-uuid --set=root 30cca32a-cbf5-4b05-8934-c6887a134162
	linux	/vmlinuz-6.11.0-29-generic root=UUID=186d206e-73e7-401c-9d8a-fad4390922f2 ro  quiet splash $vt_handoff
	initrd	/initrd.img-6.11.0-29-generic
}
"""

# NOTE: normally we will not see menuentry with nested braces,
#       but we need to support it.
BOOT_MENU_ENTRY_WITH_NESTED_BRACES = """\
menuentry 'Ubuntu' --class ubuntu --class gnu-linux --class gnu --class os $menuentry_id_option 'gnulinux-simple-186d206e-73e7-401c-9d8a-fad4390922f2' {
	recordfail
	load_video
	gfxmode $linux_gfx_mode
	insmod gzio
	if [ x$grub_platform = xxen ]; then insmod xzio; insmod lzopio; fi
    some command that uses braces {
        sub command 1
        sub command 2
    }
	insmod part_gpt
	insmod ext2
	search --no-floppy --fs-uuid --set=root 30cca32a-cbf5-4b05-8934-c6887a134162
	linux	/vmlinuz-6.11.0-29-generic root=UUID=186d206e-73e7-401c-9d8a-fad4390922f2 ro  quiet splash $vt_handoff
	initrd	/initrd.img-6.11.0-29-generic
}
"""

# ============================================================
# OTAManagedCfg
# ============================================================


class TestOTAManagedCfgInit:
    """Tests for OTAManagedCfg construction and __post_init__."""

    @pytest.mark.parametrize(
        "raw_input, expected_stripped",
        [
            pytest.param("no_strip_needed", "no_strip_needed", id="no_strip"),
            pytest.param("  leading spaces", "leading spaces", id="leading_spaces"),
            pytest.param("trailing spaces  ", "trailing spaces", id="trailing_spaces"),
            pytest.param("  both sides  ", "both sides", id="both_sides"),
            pytest.param("\nnewline\n", "newline", id="newlines"),
            pytest.param(
                "\n\t mixed whitespace \t\n", "mixed whitespace", id="mixed_whitespace"
            ),
            pytest.param("  \n  multi\nline\n  ", "multi\nline", id="multiline"),
            pytest.param("", "", id="empty_string"),
            pytest.param("   ", "", id="whitespace_only"),
        ],
    )
    def test_strips_raw_contents(self, raw_input: str, expected_stripped: str):
        cfg = OTAManagedCfg(raw_contents=raw_input, grub_version="2.06")
        assert cfg.raw_contents == expected_stripped

    @pytest.mark.parametrize(
        "raw_contents",
        [
            pytest.param(BOOT_MENU_ENTRY, id="menu_entry"),
            pytest.param(BOOT_MENU_ENTRY_WITH_NESTED_BRACES, id="nested_braces"),
            pytest.param("", id="empty"),
            pytest.param("line1\nline2\nline3", id="multiline"),
            pytest.param("special chars: !@#$%^&*()", id="special_chars"),
            pytest.param("unicode: ΑαΒβΓγΔδΕε", id="unicode"),
        ],
    )
    def test_computes_sha256_checksum(self, raw_contents: str):
        cfg = OTAManagedCfg(raw_contents=raw_contents, grub_version="2.06")

        stripped = raw_contents.strip()
        expected_digest = hashlib.sha256(stripped.encode()).hexdigest()
        assert cfg.checksum == f"sha256:{expected_digest}"

    @pytest.mark.parametrize(
        "grub_version, otaclient_version",
        [
            pytest.param("2.06", "v3.13.2", id="release"),
            pytest.param("2.12", "v3.15.6", id="newer_grub"),
            pytest.param("1.99", "v0.0.1", id="old_grub"),
        ],
    )
    def test_preserves_version_fields(self, grub_version: str, otaclient_version: str):
        cfg = OTAManagedCfg(
            raw_contents="content",
            grub_version=grub_version,
            otaclient_version=otaclient_version,
        )
        assert cfg.grub_version == grub_version
        assert cfg.otaclient_version == otaclient_version


class TestOTAManagedCfgParseFooter:
    """Tests for the static _parse_footer method."""

    @pytest.mark.parametrize(
        "footer, expected",
        [
            pytest.param(
                (
                    "# ------ OTA managed metadata ------ #\n"
                    "# otaclient_version: v3.13.2\n"
                    "# grub_version: 2.06\n"
                    "# checksum: sha256:20b4b370e9436b72dce8cf7b00c36831f425f717cac6453d89235bd4bf33e1e9\n"
                    "# ------ End of OTA managed metadata ------ #\n"
                ),
                {
                    "otaclient_version": "v3.13.2",
                    "grub_version": "2.06",
                    "checksum": "sha256:20b4b370e9436b72dce8cf7b00c36831f425f717cac6453d89235bd4bf33e1e9",
                },
                id="release_version",
            ),
            pytest.param(
                (
                    "# ------ OTA managed metadata ------ #\n"
                    "# otaclient_version: 3.13.3.dev65+ga90902447\n"
                    "# grub_version: 2.06-2ubuntu7.2\n"
                    "# checksum: sha256:974bce89216a197392b53fdf5d0f8883f107325768b6d647e89c3590882e7102\n"
                    "# ------ End of OTA managed metadata ------ #\n"
                ),
                {
                    "otaclient_version": "3.13.3.dev65+ga90902447",
                    "grub_version": "2.06-2ubuntu7.2",
                    "checksum": "sha256:974bce89216a197392b53fdf5d0f8883f107325768b6d647e89c3590882e7102",
                },
                id="dev_version_from_real_cfg",
            ),
            pytest.param(
                (
                    "# ------ OTA managed metadata ------ #\n"
                    "# key: value\n"
                    "# ------ End of OTA managed metadata ------ #\n"
                ),
                {"key": "value"},
                id="single_entry",
            ),
            pytest.param(
                (
                    "# ------ OTA managed metadata ------ #\n"
                    "# ------ End of OTA managed metadata ------ #\n"
                ),
                {},
                id="no_metadata_entries",
            ),
            pytest.param(
                "# key:\n",
                {"key": ""},
                id="empty_value",
            ),
        ],
    )
    def test_parses_well_formed_footer(self, footer: str, expected: dict):
        result = OTAManagedCfg._parse_footer(footer)
        assert result == expected

    @pytest.mark.parametrize(
        "footer",
        [
            pytest.param(
                (
                    "# ------ OTA managed metadata ------ #\n"
                    "not a comment line\n"
                    "# ------ End of OTA managed metadata ------ #\n"
                ),
                id="bare_text_between_delimiters",
            ),
            pytest.param(
                (
                    "# ------ OTA managed metadata ------ #\n"
                    "# key: value\n"
                    "invalid line\n"
                    "# ------ End of OTA managed metadata ------ #\n"
                ),
                id="invalid_line_after_valid_entry",
            ),
            pytest.param(
                "not a comment at all\n",
                id="no_comment_prefix",
            ),
            pytest.param(
                (
                    "INVALID HEADER\n"
                    "# key: value\n"
                    "# ------ End of OTA managed metadata ------ #\n"
                ),
                id="invalid_header",
            ),
            pytest.param(
                (
                    "# ------ OTA managed metadata ------ #\n"
                    "# key: value\n"
                    "INVALID TAIL\n"
                ),
                id="invalid_tail",
            ),
        ],
    )
    def test_raises_on_invalid_footer(self, footer: str):
        with pytest.raises(ValueError):
            OTAManagedCfg._parse_footer(footer)


class TestOTAManagedCfgExport:
    """Tests for the export method."""

    @pytest.mark.parametrize(
        "raw_contents, grub_version, otaclient_version",
        [
            pytest.param(
                "whatever \n\tthe\n config\n is\n",
                "2.06",
                "v3.0.0",
                id="simple_config",
            ),
            pytest.param(
                BOOT_MENU_ENTRY,
                "2.06",
                "v3.13.2",
                id="real_menu_entry",
            ),
            pytest.param(
                BOOT_MENU_ENTRY_WITH_NESTED_BRACES,
                "2.06-2ubuntu7.2",
                "3.13.3.dev65+ga90902447",
                id="nested_braces_dev_version",
            ),
        ],
    )
    def test_export_matches_expected_layout(
        self, raw_contents: str, grub_version: str, otaclient_version: str
    ):
        cfg = OTAManagedCfg(
            raw_contents=raw_contents,
            grub_version=grub_version,
            otaclient_version=otaclient_version,
        )
        expected = (
            f"{OTAManagedCfg.HEADER}"
            f"{raw_contents.strip()}\n"
            f"{OTAManagedCfg.FOOTER_HEAD}"
            f"# otaclient_version: {otaclient_version}\n"
            f"# grub_version: {grub_version}\n"
            f"# checksum: {cfg.checksum}\n"
            f"{OTAManagedCfg.FOOTER_TAIL}"
        )
        assert cfg.export() == expected


# Pre-built valid exported configs for validate tests.
_VALID_EXPORTED = OTAManagedCfg(
    raw_contents=BOOT_MENU_ENTRY, grub_version="2.06", otaclient_version="v3.0.0"
).export()

# Helper to build a crafted config string with a custom footer.
_CRAFTED_RAW = "content"
_CRAFTED_DIGEST = hashlib.sha256(_CRAFTED_RAW.encode()).hexdigest()


def _crafted_cfg(*footer_lines: str) -> str:
    footer = "".join(footer_lines)
    return (
        f"{OTAManagedCfg.HEADER}"
        f"{_CRAFTED_RAW}\n"
        f"{OTAManagedCfg.FOOTER_HEAD}"
        f"{footer}"
        f"{OTAManagedCfg.FOOTER_TAIL}"
    )


class TestOTAManagedCfgValidate:
    """Tests for validate_managed_config — the core round-trip validator."""

    @pytest.mark.parametrize(
        "config_str, expected_grub_version, expected_otaclient_version",
        [
            pytest.param(
                OTAManagedCfg(
                    raw_contents=BOOT_MENU_ENTRY,
                    grub_version="2.06",
                    otaclient_version="v3.0.0",
                ).export(),
                "2.06",
                "v3.0.0",
                id="menu_entry",
            ),
            pytest.param(
                OTAManagedCfg(
                    raw_contents=BOOT_MENU_ENTRY_WITH_NESTED_BRACES,
                    grub_version="2.06",
                    otaclient_version="v3.0.0",
                ).export(),
                "2.06",
                "v3.0.0",
                id="nested_braces",
            ),
            pytest.param(
                OTA_MANAGED_GRUB_CFG,
                "2.06-2ubuntu7.2",
                "3.13.3.dev65+ga90902447",
                id="real_ota_managed_grub_cfg",
            ),
        ],
    )
    def test_roundtrip(
        self,
        config_str: str,
        expected_grub_version: str,
        expected_otaclient_version: str,
    ):
        result = OTAManagedCfg.validate_managed_config(config_str)
        assert result is not None
        assert result.grub_version == expected_grub_version
        assert result.otaclient_version == expected_otaclient_version
        assert result.export() == config_str

    @pytest.mark.parametrize(
        "invalid_config",
        [
            pytest.param(
                "",
                id="empty_string",
            ),
            pytest.param(
                _VALID_EXPORTED.replace(OTAManagedCfg.HEADER, ""),
                id="header_missing",
            ),
            pytest.param(
                _VALID_EXPORTED.replace(OTAManagedCfg.FOOTER_HEAD, ""),
                id="footer_head_missing",
            ),
            pytest.param(
                _VALID_EXPORTED.replace(OTAManagedCfg.FOOTER_TAIL, ""),
                id="footer_tail_missing",
            ),
            pytest.param(
                _VALID_EXPORTED.replace("# checksum: sha256:", "# checksum: sha256:0"),
                id="checksum_corrupted",
            ),
            pytest.param(
                _VALID_EXPORTED.replace(
                    "vmlinuz-6.11.0-29-generic", "vmlinuz-not_this_one"
                ),
                id="content_tampered",
            ),
            pytest.param(
                _crafted_cfg(
                    "# otaclient_version: v1.0.0\n",
                    f"# checksum: sha256:{_CRAFTED_DIGEST}\n",
                ),
                id="metadata_key_missing_grub_version",
            ),
            pytest.param(
                _crafted_cfg(
                    "# otaclient_version: v1.0.0\n",
                    "# grub_version: 2.06\n",
                    "# checksum: nosuchalgo:abc\n",
                ),
                id="unsupported_hash_algorithm",
            ),
            pytest.param(
                _crafted_cfg("bad line without hash prefix\n"),
                id="invalid_line_in_footer",
            ),
        ],
    )
    def test_returns_none_for_invalid_config(self, invalid_config: str):
        assert OTAManagedCfg.validate_managed_config(invalid_config) is None


# ============================================================
# OTASlotBootID
# ============================================================


class TestOTASlotBootID:
    @pytest.mark.parametrize(
        "slot, expected_value, expected_suffix",
        [
            pytest.param(OTASlotBootID.slot_a, "ota-slot_a", "_a", id="slot_a"),
            pytest.param(OTASlotBootID.slot_b, "ota-slot_b", "_b", id="slot_b"),
        ],
    )
    def test_slot_properties(
        self, slot: OTASlotBootID, expected_value: str, expected_suffix: str
    ):
        assert slot == expected_value
        assert slot.get_suffix() == expected_suffix
        assert isinstance(slot, str)


# ============================================================
# SlotInfo
# ============================================================


class TestSlotInfo:
    @pytest.mark.parametrize(
        "dev, uuid, parttype, slot_id",
        [
            pytest.param(
                "/dev/sda1",
                "cb7519f4-924b-4f2c-ac30-9318e31cf64e",
                "ext4",
                None,
                id="without_slot_id",
            ),
            pytest.param(
                "/dev/sda2",
                "d9a87440-5dcf-4fa1-994a-e84f8a9ae9df",
                "ext4",
                OTASlotBootID.slot_a,
                id="with_slot_a",
            ),
            pytest.param(
                "/dev/nvme0n1p3",
                "ba7ed9ca-0188-4f66-bb01-b1ac990f2a31",
                "btrfs",
                OTASlotBootID.slot_b,
                id="with_slot_b_nvme",
            ),
        ],
    )
    def test_from_partinfo(
        self, dev: str, uuid: str, parttype: str, slot_id: OTASlotBootID | None
    ):
        partinfo = PartitionInfo(dev=dev, uuid=uuid, parttype=parttype)
        slot = SlotInfo.from_partinfo(partinfo, slot_id)

        assert slot.dev == dev
        assert slot.uuid == uuid
        assert slot.slot_id is slot_id


# ============================================================
# read_fstab_dict
# ============================================================


SAMPLE_FSTAB = """\
UUID=cb7519f4-924b-4f2c-ac30-9318e31cf64e	/	ext4	errors=remount-ro	0	1
UUID=d9a87440-5dcf-4fa1-994a-e84f8a9ae9df	/boot	ext4	defaults	0	1
UUID=CB4C-72D3	/boot/efi	vfat	defaults	0	1
UUID=ba7ed9ca-0188-4f66-bb01-b1ac990f2a31   /media/custom_mount   ext4  defaults    0   1
/dev/sdb1   /media/custom_mount2    ext4    defaults    0   1
"""

# Expected parsed results for each mount point in SAMPLE_FSTAB.
# Keyed by mount_point -> (file_system, type, options, dump, pass).
SAMPLE_FSTAB_EXPECTED: dict[str, tuple[str, str, str, str, str]] = {
    "/": (
        "UUID=cb7519f4-924b-4f2c-ac30-9318e31cf64e",
        "ext4",
        "errors=remount-ro",
        "0",
        "1",
    ),
    "/boot": (
        "UUID=d9a87440-5dcf-4fa1-994a-e84f8a9ae9df",
        "ext4",
        "defaults",
        "0",
        "1",
    ),
    "/boot/efi": ("UUID=CB4C-72D3", "vfat", "defaults", "0", "1"),
    "/media/custom_mount": (
        "UUID=ba7ed9ca-0188-4f66-bb01-b1ac990f2a31",
        "ext4",
        "defaults",
        "0",
        "1",
    ),
    "/media/custom_mount2": ("/dev/sdb1", "ext4", "defaults", "0", "1"),
}


class TestReadFstabDict:
    @pytest.mark.parametrize(
        "mount_point, expected",
        [
            pytest.param(mp, expected, id=mp)
            for mp, expected in SAMPLE_FSTAB_EXPECTED.items()
        ],
    )
    def test_parses_sample_fstab(
        self,
        mount_point: str,
        expected: tuple[str, str, str, str, str],
    ):
        result = read_fstab_dict(SAMPLE_FSTAB)
        assert mount_point in result

        match = result[mount_point]
        file_system, fstype, options, dump, pass_ = expected
        assert match.group("file_system") == file_system
        assert match.group("mount_point") == mount_point
        assert match.group("type") == fstype
        assert match.group("options") == options
        assert match.group("dump") == dump
        assert match.group("pass") == pass_

    @pytest.mark.parametrize(
        "fstab_input, expected_mount_points",
        [
            pytest.param(
                "",
                set(),
                id="empty_input",
            ),
            pytest.param(
                "# comment\n# another comment\n",
                set(),
                id="only_comments",
            ),
            pytest.param(
                "UUID=abc /  ext4  defaults  0  1\ngarbage line\n",
                {"/"},
                id="skips_malformed_lines",
            ),
            pytest.param(
                "   UUID=abc  /mnt  ext4  defaults  0  0\n",
                {"/mnt"},
                id="leading_whitespace_accepted",
            ),
            pytest.param(
                "UUID=abc  /  ext4  defaults  0\n",
                set(),
                id="incomplete_entry_missing_pass",
            ),
        ],
    )
    def test_edge_cases(self, fstab_input: str, expected_mount_points: set):
        result = read_fstab_dict(fstab_input)
        assert set(result.keys()) == expected_mount_points
