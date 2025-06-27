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

from pathlib import Path

from pytest_mock import MockerFixture

from ota_metadata.legacy2.metadata import OTAMetadata
from otaclient.create_standby.delta_gen import RebuildDeltaWithBaseFileTable

from .conftest import SlotAB


def test_rebuild_mode_with_base_file_table_assist(
    ab_slots_for_rebuild: SlotAB,
    ota_metadata_inst: OTAMetadata,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    mocked_report_que = mocker.MagicMock()
    mocked_report_que.put_nowait = lambda _: None
    resource_dir = tmp_path / ".ota-tmp"

    RebuildDeltaWithBaseFileTable(
        ota_metadata=ota_metadata_inst,
        delta_src=ab_slots_for_rebuild.slot_a,
        copy_dst=resource_dir,
        status_report_queue=mocked_report_que,
        session_id="session_id",
    ).process_slot(str(ota_metadata_inst._fst_db))


def test_rebuild_mode_with_full_disk_scan():
    pass


def test_inplace_mode_with_full_disk_scan():
    pass


# NOTE: inplace mode with base file_table assist is tested in test_update_slot
# def test_inplace_mode_with_base_file_assist():
#     pass
