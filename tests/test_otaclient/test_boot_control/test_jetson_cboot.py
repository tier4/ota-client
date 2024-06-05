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
"""
NOTE: this test file only test utils used in jetson-cboot.
"""


from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from otaclient.app.boot_control import _jetson_cboot
from otaclient.app.boot_control._jetson_cboot import _CBootControl
from otaclient.app.boot_control._jetson_common import (
    BSPVersion,
    FirmwareBSPVersion,
    SlotID,
    parse_bsp_version,
    update_extlinux_cfg,
)
from tests.conftest import TEST_DIR

logger = logging.getLogger(__name__)

MODULE_NAME = _jetson_cboot.__name__
TEST_DATA_DIR = TEST_DIR / "data"


def test_SlotID():
    SlotID("0")
    SlotID("1")
    with pytest.raises(ValueError):
        SlotID("abc")


class TestBSPVersion:

    @pytest.mark.parametrize(
        ["_in", "expected"],
        (
            ("R32.5.2", BSPVersion(32, 5, 2)),
            ("R32.6.1", BSPVersion(32, 6, 1)),
            ("R35.4.1", BSPVersion(35, 4, 1)),
        ),
    )
    def test_parse(self, _in: str, expected: BSPVersion):
        assert BSPVersion.parse(_in) == expected

    @pytest.mark.parametrize(
        ["_in", "expected"],
        (
            (BSPVersion(32, 5, 2), "R32.5.2"),
            (BSPVersion(32, 6, 1), "R32.6.1"),
            (BSPVersion(35, 4, 1), "R35.4.1"),
        ),
    )
    def test_dump(self, _in: BSPVersion, expected: str):
        assert BSPVersion.dump(_in) == expected


@pytest.mark.parametrize(
    ["_in", "expected"],
    (
        (
            {
                "slot_a": None,
                "slot_b": None,
            },
            FirmwareBSPVersion(),
        ),
        (
            {
                "slot_a": None,
                "slot_b": "R32.6.1",
            },
            FirmwareBSPVersion(slot_a=None, slot_b=BSPVersion(32, 6, 1)),
        ),
        (
            {
                "slot_a": "R32.5.2",
                "slot_b": "R32.6.1",
            },
            FirmwareBSPVersion(
                slot_a=BSPVersion(32, 5, 2), slot_b=BSPVersion(32, 6, 1)
            ),
        ),
    ),
)
def test_FirmwareBSPVersion(_in: dict[str, Any], expected: FirmwareBSPVersion):
    assert FirmwareBSPVersion.model_validate(_in) == expected


@pytest.mark.parametrize(
    ["_in", "expected"],
    (
        (
            (
                "# R32 (release), REVISION: 6.1, GCID: 27863751, BOARD: t186ref, EABI: aarch64, DATE: Mon Jul 26 19:36:31 UTC 2021",
                BSPVersion(32, 6, 1),
            ),
            (
                "# R35 (release), REVISION: 4.1, GCID: 33958178, BOARD: t186ref, EABI: aarch64, DATE: Tue Aug  1 19:57:35 UTC 2023",
                BSPVersion(35, 4, 1),
            ),
        )
    ),
)
def test_parse_bsp_version(_in: str, expected: BSPVersion):
    assert parse_bsp_version(_in) == expected


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
