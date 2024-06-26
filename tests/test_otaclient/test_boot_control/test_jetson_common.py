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

from otaclient.app.boot_control._jetson_common import (
    BSPVersion,
    FirmwareBSPVersion,
    SlotID,
    SLOT_A,
    SLOT_B,
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


class TestFirmwareBSPVersion:

    @pytest.mark.parametrize(
        "_in, _slot, _bsp_ver, _expect",
        (
            (
                FirmwareBSPVersion(),
                SLOT_A,
                BSPVersion(32, 6, 1),
                FirmwareBSPVersion(slot_a=BSPVersion(32, 6, 1)),
            ),
            (
                FirmwareBSPVersion(
                    slot_a=BSPVersion(32, 5, 1), slot_b=BSPVersion(32, 6, 1)
                ),
                SLOT_B,
                None,
                FirmwareBSPVersion(slot_a=BSPVersion(32, 5, 1), slot_b=None),
            ),
            (
                FirmwareBSPVersion(
                    slot_a=BSPVersion(32, 5, 1), slot_b=BSPVersion(32, 6, 1)
                ),
                SLOT_A,
                None,
                FirmwareBSPVersion(slot_a=None, slot_b=BSPVersion(32, 6, 1)),
            ),
        ),
    )
    def test_set_by_slot(
        self,
        _in: FirmwareBSPVersion,
        _slot: SlotID,
        _bsp_ver: BSPVersion | None,
        _expect: FirmwareBSPVersion,
    ):
        _in.set_by_slot(_slot, _bsp_ver)
        assert _in == _expect

    @pytest.mark.parametrize(
        "_in, _slot, _expect",
        (
            (
                FirmwareBSPVersion(),
                SLOT_A,
                None,
            ),
            (
                FirmwareBSPVersion(
                    slot_a=BSPVersion(32, 5, 1), slot_b=BSPVersion(32, 6, 1)
                ),
                SLOT_B,
                BSPVersion(32, 6, 1),
            ),
            (
                FirmwareBSPVersion(
                    slot_a=BSPVersion(32, 5, 1), slot_b=BSPVersion(32, 6, 1)
                ),
                SLOT_A,
                BSPVersion(32, 5, 1),
            ),
        ),
    )
    def test_get_by_slot(
        self,
        _in: FirmwareBSPVersion,
        _slot: SlotID,
        _expect: BSPVersion | None,
    ):
        assert _in.get_by_slot(_slot) == _expect

    @pytest.mark.parametrize(
        "_in",
        (
            (FirmwareBSPVersion()),
            (FirmwareBSPVersion(slot_a=BSPVersion(32, 5, 1))),
            (
                FirmwareBSPVersion(
                    slot_a=BSPVersion(35, 4, 1), slot_b=BSPVersion(35, 5, 0)
                )
            ),
        ),
    )
    def test_load_and_dump(self, _in: FirmwareBSPVersion):
        assert FirmwareBSPVersion.model_validate_json(_in.model_dump_json()) == _in


class TestFirmwareBSPVersionControl:

    def test_init(self):
        pass

    def test_write_to_file(self):
        pass

    def test_get_set_current_slot(self):
        pass

    def test_get_set_standby_slot(self):
        pass


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
