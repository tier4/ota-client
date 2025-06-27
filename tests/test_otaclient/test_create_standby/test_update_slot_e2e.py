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

import logging
import os
from hashlib import sha256
from pathlib import Path

from ota_metadata.legacy2.metadata import OTAMetadata
from otaclient.create_standby.delta_gen import (
    InPlaceDeltaWithBaseFileTable,
)
from otaclient.create_standby.update_slot import UpdateStandbySlot
from otaclient_common import replace_root

from .conftest import SlotAB, verify_resources
from .test_delta_gen_e2e import MockedQue

logger = logging.getLogger(__name__)


def verify_slot(_ota_metadata_inst: OTAMetadata, slot: Path):
    logger.info("verify all regular files ...")
    for _entry in _ota_metadata_inst.iter_regular_entries():
        _f_at_slot = Path(
            replace_root(
                _entry["path"],
                "/",
                slot,
            )
        )
        assert _f_at_slot.is_file()
        assert sha256(_f_at_slot.read_bytes()).digest() == _entry["digest"]
        assert _f_at_slot.stat().st_uid == _entry["uid"]
        assert _f_at_slot.stat().st_gid == _entry["gid"]
        assert _f_at_slot.stat().st_mode == _entry["mode"]

    logger.info("verify all directories ...")
    for _entry in _ota_metadata_inst.iter_dir_entries():
        _d_at_slot = Path(
            replace_root(
                _entry["path"],
                "/",
                slot,
            )
        )
        assert _d_at_slot.is_dir()
        assert _d_at_slot.stat().st_uid == _entry["uid"]
        assert _d_at_slot.stat().st_gid == _entry["gid"]
        assert _d_at_slot.stat().st_mode == _entry["mode"]

    logger.info("verify all non-regular files ...")
    for _entry in _ota_metadata_inst.iter_non_regular_entries():
        _f_at_slot = Path(
            replace_root(
                _entry["path"],
                "/",
                slot,
            )
        )
        # NOTE: for old OTA image, non-regular-file category only has symlink
        if _f_at_slot.is_symlink():
            assert _entry["meta"] and os.readlink(_f_at_slot) == _entry["meta"].decode()
        else:
            raise ValueError(f"unknown non-regular file {_f_at_slot}")


def test_update_slot_with_inplace_mode_with_full_disk_scan(
    ab_slots_for_inplace: SlotAB,
    ota_metadata_inst: OTAMetadata,
    resource_dir: Path,
) -> None:
    logger.info("start to test inplace mode with base file_table assist ...")
    InPlaceDeltaWithBaseFileTable(
        ota_metadata=ota_metadata_inst,
        delta_src=ab_slots_for_inplace.slot_b,
        copy_dst=resource_dir,
        status_report_queue=MockedQue,
        session_id="session_id",
    ).process_slot(str(ota_metadata_inst._fst_db))
    logger.info("verify resource folder ...")
    verify_resources(ota_metadata_inst, resource_dir)

    logger.info("start to update slot ...")
    UpdateStandbySlot(
        ota_metadata=ota_metadata_inst,
        standby_slot_mount_point=str(ab_slots_for_inplace.slot_b),
        status_report_queue=MockedQue,
        resource_dir=resource_dir,
        session_id="session_id",
    ).update_slot()
    logger.info("verify the updated slot against file_table ...")
    verify_slot(ota_metadata_inst, ab_slots_for_inplace.slot_b)
