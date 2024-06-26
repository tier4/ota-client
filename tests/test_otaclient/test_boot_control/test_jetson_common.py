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
)


def test_SlotID(_in: Any, _expect: SlotID | None, _exc: Exception | None):
    pass


class TestBSPVersion:

    def test_parse(self, _in: Any, _expect: BSPVersion | None, _exc: Exception | None):
        pass

    def test_dump(self, _in: BSPVersion, _expect: str):
        pass


class TestFirmwareBSPVersion:

    def test_init(self, slot_a_ver: BSPVersion | None, slot_b_ver: BSPVersion | None):
        pass

    def test_set_by_slot(
        self,
        _in: FirmwareBSPVersion,
        _slot: SlotID,
        _bsp_ver: BSPVersion | None,
        _expect: FirmwareBSPVersion,
    ):
        pass

    def test_get_by_slot(
        self,
        _in: FirmwareBSPVersion,
        _slot: SlotID,
        _exp: BSPVersion | None,
    ):
        pass

    def test_load_and_dump(self, _in: FirmwareBSPVersion):
        pass


class TestFirmwareBSPVersionControl:

    def test_init(self):
        pass

    def test_write_to_file(self):
        pass

    def test_get_set_current_slot(self):
        pass

    def test_get_set_standby_slot(self):
        pass


def test_parse_nv_tegra_release(_in: str, _expect: BSPVersion):
    pass


def test_detect_rootfs_bsp_version(tmp_path: Path):
    pass


def test_update_extlinux_cfg(_in: str, _partuuid: str, _expect: str):
    pass


class Test_copy_standby_slot_boot_to_internal_emmc:

    @pytest.fixture(autouse=True)
    def setup_test(self):
        pass

    def test_copy_standby_slot_boot_to_internal_emmc(self):
        pass


class Test_preserve_ota_config_files_to_standby:

    @pytest.fixture(autouse=True)
    def setup_test(self):
        pass

    def test_preserve_ota_config_files_to_standby(self):
        pass
