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
"""Tests for Jetson device boot control implementation common."""


from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from otaclient.boot_control._jetson_common import (
    SLOT_A,
    SLOT_B,
    BSPVersion,
    FirmwareBSPVersionControl,
    SlotBSPVersion,
    SlotID,
    detect_external_rootdev,
    get_nvbootctrl_conf_tnspec,
    get_partition_devpath,
    parse_nv_tegra_release,
    update_extlinux_cfg,
)
from tests.conftest import TEST_DIR

TEST_DATA_DIR = TEST_DIR / "data"


@pytest.mark.parametrize(
    "_in, _expect, _exc",
    (
        (SLOT_A, SLOT_A, None),
        (SLOT_B, SLOT_B, None),
        ("0", SLOT_A, None),
        ("1", SLOT_B, None),
        ("not_a_valid_slot_id", None, ValueError),
    ),
)
def test_slot_id(_in: Any, _expect: SlotID | None, _exc: type[Exception] | None):
    def _test():
        assert SlotID(_in) == _expect

    if _exc:
        with pytest.raises(_exc):
            return _test()
    else:
        _test()


class TestBSPVersion:

    @pytest.mark.parametrize(
        "_in, _expect, _exc",
        (
            ("R32.6.1", BSPVersion(32, 6, 1), None),
            ("r32.6.1", BSPVersion(32, 6, 1), None),
            ("32.6.1", BSPVersion(32, 6, 1), None),
            ("R35.4.1", BSPVersion(35, 4, 1), None),
            ("1.22.333", BSPVersion(1, 22, 333), None),
            ("not_a_valid_bsp_ver", None, AssertionError),
            (123, None, ValueError),
        ),
    )
    def test_parse(
        self, _in: Any, _expect: BSPVersion | None, _exc: type[Exception] | None
    ):
        def _test():
            assert BSPVersion.parse(_in) == _expect

        if _exc:
            with pytest.raises(_exc):
                _test()
        else:
            _test()

    @pytest.mark.parametrize(
        "_in, _expect",
        (
            (BSPVersion(35, 4, 1), "R35.4.1"),
            (BSPVersion(32, 6, 1), "R32.6.1"),
            (BSPVersion(1, 22, 333), "R1.22.333"),
        ),
    )
    def test_dump(self, _in: BSPVersion, _expect: str):
        assert _in.dump() == _expect


class TestSlotBSPVersion:

    @pytest.mark.parametrize(
        "_in, _slot, _bsp_ver, _expect",
        (
            (
                SlotBSPVersion(),
                SLOT_A,
                BSPVersion(32, 6, 1),
                SlotBSPVersion(slot_a=BSPVersion(32, 6, 1)),
            ),
            (
                SlotBSPVersion(
                    slot_a=BSPVersion(32, 5, 1), slot_b=BSPVersion(32, 6, 1)
                ),
                SLOT_B,
                None,
                SlotBSPVersion(slot_a=BSPVersion(32, 5, 1), slot_b=None),
            ),
            (
                SlotBSPVersion(
                    slot_a=BSPVersion(32, 5, 1), slot_b=BSPVersion(32, 6, 1)
                ),
                SLOT_A,
                None,
                SlotBSPVersion(slot_a=None, slot_b=BSPVersion(32, 6, 1)),
            ),
        ),
    )
    def test_set_by_slot(
        self,
        _in: SlotBSPVersion,
        _slot: SlotID,
        _bsp_ver: BSPVersion | None,
        _expect: SlotBSPVersion,
    ):
        _in.set_by_slot(_slot, _bsp_ver)
        assert _in == _expect

    @pytest.mark.parametrize(
        "_in, _slot, _expect",
        (
            (
                SlotBSPVersion(),
                SLOT_A,
                None,
            ),
            (
                SlotBSPVersion(
                    slot_a=BSPVersion(32, 5, 1), slot_b=BSPVersion(32, 6, 1)
                ),
                SLOT_B,
                BSPVersion(32, 6, 1),
            ),
            (
                SlotBSPVersion(
                    slot_a=BSPVersion(32, 5, 1), slot_b=BSPVersion(32, 6, 1)
                ),
                SLOT_A,
                BSPVersion(32, 5, 1),
            ),
        ),
    )
    def test_get_by_slot(
        self,
        _in: SlotBSPVersion,
        _slot: SlotID,
        _expect: BSPVersion | None,
    ):
        assert _in.get_by_slot(_slot) == _expect

    @pytest.mark.parametrize(
        "_in",
        (
            (SlotBSPVersion()),
            (SlotBSPVersion(slot_a=BSPVersion(32, 5, 1))),
            (SlotBSPVersion(slot_a=BSPVersion(35, 4, 1), slot_b=BSPVersion(35, 5, 0))),
        ),
    )
    def test_load_and_dump(self, _in: SlotBSPVersion):
        assert SlotBSPVersion.model_validate_json(_in.model_dump_json()) == _in


class TestFirmwareBSPVersionControl:

    @pytest.fixture(autouse=True)
    def setup_test(self, tmp_path: Path):
        self.test_fw_bsp_vf = tmp_path / "firmware_bsp_version"
        self.slot_b_ver = BSPVersion(35, 5, 0)
        self.slot_a_ver = BSPVersion(35, 4, 1)

    def test_init(self):
        self.test_fw_bsp_vf.write_text(
            SlotBSPVersion(slot_b=self.slot_b_ver).model_dump_json()
        )

        loaded = FirmwareBSPVersionControl(
            SLOT_A,
            self.slot_a_ver,
            current_bsp_version_file=self.test_fw_bsp_vf,
        )

        # NOTE: FirmwareBSPVersionControl will not use the information for current slot.
        assert loaded.current_slot_bsp_ver == self.slot_a_ver
        assert loaded.standby_slot_bsp_ver == self.slot_b_ver

    def test_write_to_file(self):
        self.test_fw_bsp_vf.write_text(
            SlotBSPVersion(slot_b=self.slot_b_ver).model_dump_json()
        )
        loaded = FirmwareBSPVersionControl(
            SLOT_A,
            self.slot_a_ver,
            current_bsp_version_file=self.test_fw_bsp_vf,
        )
        loaded.write_to_file(self.test_fw_bsp_vf)

        assert (
            self.test_fw_bsp_vf.read_text()
            == SlotBSPVersion(
                slot_a=self.slot_a_ver, slot_b=self.slot_b_ver
            ).model_dump_json()
        )


@pytest.mark.parametrize(
    "_in, _expect",
    (
        (
            "# R32 (release), REVISION: 6.1, GCID: 27863751, BOARD: t186ref, EABI: aarch64, DATE: Mon Jul 26 19:36:31 UTC 2021",
            BSPVersion(32, 6, 1),
        ),
        (
            "# R35 (release), REVISION: 4.1, GCID: 33958178, BOARD: t186ref, EABI: aarch64, DATE: Tue Aug  1 19:57:35 UTC 2023",
            BSPVersion(35, 4, 1),
        ),
        (
            "# R35 (release), REVISION: 5.0, GCID: 35550185, BOARD: t186ref, EABI: aarch64, DATE: Tue Feb 20 04:46:31 UTC 2024",
            BSPVersion(35, 5, 0),
        ),
    ),
)
def test_parse_nv_tegra_release(_in: str, _expect: BSPVersion):
    assert parse_nv_tegra_release(_in) == _expect


@pytest.mark.parametrize(
    ["_template_f", "_updated_f", "partuuid"],
    (
        (
            "extlinux.conf-r35.4.1-template1",
            "extlinux.conf-r35.4.1-updated1",
            "11aa-bbcc-22dd",
        ),
        (
            "extlinux.conf-r35.4.1-template2",
            "extlinux.conf-r35.4.1-updated2",
            "11aa-bbcc-22dd",
        ),
    ),
)
def test_update_extlinux_conf(_template_f: Path, _updated_f: Path, partuuid: str):
    _in = (TEST_DATA_DIR / _template_f).read_text()
    _expected = (TEST_DATA_DIR / _updated_f).read_text()
    assert update_extlinux_cfg(_in, partuuid) == _expected


@pytest.mark.parametrize(
    "parent_devpath, is_external_rootdev",
    (
        ("/dev/mmcblk0", False),
        ("/dev/mmcblk1", True),
        ("/dev/sda", True),
        ("/dev/nvme0n1", True),
    ),
)
def test_detect_external_rootdev(parent_devpath, is_external_rootdev):
    assert detect_external_rootdev(parent_devpath) is is_external_rootdev


@pytest.mark.parametrize(
    "parent_devpath, partition_id, expected",
    (
        ("/dev/mmcblk0", 1, "/dev/mmcblk0p1"),
        ("/dev/mmcblk1", 1, "/dev/mmcblk1p1"),
        ("/dev/sda", 1, "/dev/sda1"),
        ("/dev/nvme0n1", 1, "/dev/nvme0n1p1"),
    ),
)
def test_get_partition_devpath(parent_devpath, partition_id, expected):
    assert get_partition_devpath(parent_devpath, partition_id) == expected


@pytest.mark.parametrize(
    "nvbooctrl_conf, expected",
    (
        (
            """\
TNSPEC 2888-400-0004-M.0-1-2-jetson-xavier-rqx580-
TEGRA_CHIPID 0x19
TEGRA_OTA_BOOT_DEVICE /dev/mmcblk0boot0
TEGRA_OTA_GPT_DEVICE /dev/mmcblk0boot1
""",
            "2888-400-0004-M.0-1-2-jetson-xavier-rqx580-",
        ),
        (
            """\
TNSPEC 3701-500-0005-A.0-1-0-cti-orin-agx-agx201-00-
TEGRA_CHIPID 0x23
TEGRA_OTA_BOOT_DEVICE /dev/mtdblock0
TEGRA_OTA_GPT_DEVICE /dev/mtdblock0      
""",
            "3701-500-0005-A.0-1-0-cti-orin-agx-agx201-00-",
        ),
        (
            "not a nvbooctrl file",
            None,
        ),
    ),
)
def test_get_nvbootctrl_conf_tnspec(nvbooctrl_conf, expected):
    assert get_nvbootctrl_conf_tnspec(nvbooctrl_conf) == expected
