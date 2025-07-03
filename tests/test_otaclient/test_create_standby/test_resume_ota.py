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
import queue
from pathlib import Path
from typing import cast

import pytest
import pytest_mock

from ota_metadata.legacy2.metadata import OTAMetadata
from otaclient.create_standby.delta_gen import (
    InPlaceDeltaWithBaseFileTable,
)
from otaclient.create_standby.resume_ota import ResourceScanner

from .conftest import SlotAB

logger = logging.getLogger(__name__)

DELTA_GEN_MODULE = "otaclient.create_standby.delta_gen"
DELTA_GEN_FULL_DISK_SCAN_BASE = f"{DELTA_GEN_MODULE}.DeltaGenFullDiskScan"


class _MockedQue:
    @staticmethod
    def put_nowait(_): ...


MockedQue = cast(queue.Queue, _MockedQue)


@pytest.fixture(autouse=True)
def mocked_full_disk_scan_mode(mocker: pytest_mock.MockerFixture):
    # NOTE: to let full disk scan mode can get all the resources, only for test
    mocker.patch(f"{DELTA_GEN_FULL_DISK_SCAN_BASE}.EXCLUDE_PATHS", {})
    mocker.patch(f"{DELTA_GEN_MODULE}.MAX_FOLDER_DEEPTH", 2**32)
    mocker.patch(f"{DELTA_GEN_MODULE}.MAX_FILENUM_PER_FOLDER", 2**64)


def test_resume_ota(
    ab_slots_for_inplace: SlotAB,
    ota_metadata_inst: OTAMetadata,
    resource_dir: Path,
) -> None:
    logger.info("process slot and generating OTA resources ...")
    InPlaceDeltaWithBaseFileTable(
        ota_metadata=ota_metadata_inst,
        delta_src=ab_slots_for_inplace.slot_b,
        copy_dst=resource_dir,
        status_report_queue=MockedQue,
        session_id="session_id",
    ).process_slot(str(ota_metadata_inst._fst_db))

    logger.info("scan through the generated resources ...")
    ResourceScanner(
        ota_metadata=ota_metadata_inst,
        resource_dir=resource_dir,
        status_report_queue=MockedQue,
        session_id="session_id",
    ).resume_ota()
