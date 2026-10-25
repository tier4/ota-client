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
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest
import pytest_mock
from ota_image_libs.v1.file_table.db import FileTableDBHelper

from otaclient.configs.cfg import cfg
from otaclient.create_standby._common import ResourcesDigestWithSize
from otaclient.create_standby.delta_gen import InPlaceDeltaWithBaseFileTable
from otaclient.create_standby.resume_ota import ResourceScanner
from tests.conftest import SlotAB

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


@pytest.mark.parametrize("verify_by_hash", (True, False))
def test_resume_ota(
    verify_by_hash: bool,
    ab_slots_for_inplace: SlotAB,
    fst_db_helper: FileTableDBHelper,
    resource_dir: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    mocker.patch.object(cfg, "VERIFY_STANDBY_RESOURCES_BY_HASH", verify_by_hash)
    logger.info("process slot and generating OTA resources ...")
    _all_digests = ResourcesDigestWithSize.from_iterable(
        fst_db_helper.select_all_digests_with_size()
    )

    InPlaceDeltaWithBaseFileTable(
        file_table_db_helper=fst_db_helper,
        all_resource_digests=_all_digests,
        delta_src=ab_slots_for_inplace.slot_b,
        copy_dst=resource_dir,
        status_report_queue=MockedQue,
        session_id="session_id",
    ).process_slot(str(fst_db_helper.db_f))

    logger.info("scan through the generated resources ...")
    ResourceScanner(
        all_resource_digests=_all_digests,
        resource_dir=resource_dir,
        status_report_queue=MockedQue,
        session_id="session_id",
    ).resume_ota()

    # with the standby slot intact, reusing by size collects the same, valid resources
    for _resource in resource_dir.iterdir():
        assert sha256(_resource.read_bytes()).hexdigest() == _resource.name


def test_resource_scanner_reuse_by_size(
    tmp_path: Path, mocker: pytest_mock.MockerFixture
) -> None:
    mocker.patch.object(cfg, "VERIFY_STANDBY_RESOURCES_BY_HASH", False)
    contents = {"good": b"good resource", "truncated": b"truncated resource"}
    digests = {_name: sha256(_c).digest() for _name, _c in contents.items()}
    (tmp_path / digests["good"].hex()).write_bytes(contents["good"])
    (tmp_path / digests["truncated"].hex()).write_bytes(contents["truncated"][:5])

    _all_digests = ResourcesDigestWithSize.from_iterable(
        (digests[_name], len(_c)) for _name, _c in contents.items()
    )
    ResourceScanner(
        all_resource_digests=_all_digests,
        resource_dir=tmp_path,
        status_report_queue=MockedQue,
        session_id="session_id",
    ).resume_ota()

    # the size-matched resource is taken, the truncated one is removed and still needed
    assert (tmp_path / digests["good"].hex()).is_file()
    assert not (tmp_path / digests["truncated"].hex()).exists()
    assert digests["good"] not in _all_digests
    assert digests["truncated"] in _all_digests
