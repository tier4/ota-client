# Copyright 2026 TIER IV, INC. All rights reserved.
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
"""The current slot's records, as a parameter set (no slot switch) rewrites them."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from otaclient._types import OTAStatus, VersionDetail
from otaclient.boot_control._ota_status_control import OTAStatusFilesControl

DETAIL = VersionDetail(release_name="params", release_id="rid", image_id="pimg")


@pytest.fixture
def control(tmp_path: Path) -> OTAStatusFilesControl:
    _current = tmp_path / "slot_a"
    _current.mkdir()
    # a full OTA failed before: FAILURE with its reason, the base's release
    (_current / "status").write_text("FAILURE")
    (_current / "slot_in_use").write_text("slot_a")
    (_current / "version").write_text("1.0.0")
    (_current / "version_detail").write_text(
        json.dumps({"release_name": "base", "release_id": "b", "image_id": "base"})
    )
    (_current / "failure_info").write_text("{}")
    return OTAStatusFilesControl(
        active_slot="slot_a",
        standby_slot="slot_b",
        current_ota_status_dir=_current,
        standby_ota_status_dir=tmp_path / "slot_b",
        finalize_switching_boot=lambda: True,
    )


def test_a_set_applied_makes_the_slot_success_with_its_release(
    control: OTAStatusFilesControl,
):
    control.on_parameter_set_applied(version="2026.10.29-1", version_detail=DETAIL)

    _dir = control.current_ota_status_dir
    assert (_dir / "status").read_text() == OTAStatus.SUCCESS.name
    assert not (_dir / "failure_info").exists(), (
        "the old failure is not this slot's now"
    )
    assert control.load_active_slot_version() == "2026.10.29-1"
    assert control.load_active_slot_version_detail() == DETAIL
    assert not (control.standby_ota_status_dir / "version").exists()


def test_a_release_without_detail_leaves_no_old_detail(control: OTAStatusFilesControl):
    control.on_parameter_set_applied(version="2026.10.29-1", version_detail=None)
    assert control.load_active_slot_version_detail() is None


def test_the_base_records_come_back(control: OTAStatusFilesControl):
    control.on_parameter_set_applied(version="2026.10.29-1", version_detail=DETAIL)
    _base = VersionDetail(release_name="base", release_id="b", image_id="base")
    control.store_current_version(version="1.0.0", version_detail=_base)
    assert control.load_active_slot_version() == "1.0.0"
    assert control.load_active_slot_version_detail() == _base
