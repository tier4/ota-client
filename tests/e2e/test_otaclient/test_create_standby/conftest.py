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
"""Local fixtures/helpers for create_standby e2e tests.

The shared file_table fixtures (`fst_db_helper`, `ab_slots_for_inplace`,
`resource_dir`) and the `SlotAB` type live in the top-level
`tests/conftest.py` (the /ota-image fixture tree they build from is baked into
the test container by docker/test_base/Dockerfile). Only the rebuild-mode slot
fixture and the resource-verification helper are specific to these e2e tests.
"""

from __future__ import annotations

import logging
import shutil
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.conftest import OTA_IMAGE_DATA_DIR, SlotAB  # noqa: F401

if TYPE_CHECKING:
    from ota_image_libs.v1.file_table.db import FileTableDBHelper

logger = logging.getLogger(__name__)


@pytest.fixture
def ab_slots_for_rebuild(tmp_path: Path) -> SlotAB:
    logger.info("prepare simple a/b slots for rebuild mode ...")
    slot_a = tmp_path / "slot_a"
    slot_b = tmp_path / "slot_b"
    logger.info(f"prepare simple a/b slots for rebuild mode: {slot_a=}, {slot_b=}")

    slot_b.mkdir(exist_ok=True, parents=True)
    shutil.copytree(OTA_IMAGE_DATA_DIR, slot_a, symlinks=True)
    return SlotAB(slot_a, slot_b)


def verify_resources(_fst_db_helper: FileTableDBHelper, resource_dir: Path) -> None:
    """Verify that all resources are prepared."""
    from otaclient_common.thread_safe_container import ShardedThreadSafeDict

    logger.info(f"verify against {resource_dir=} ...")
    _errs = []
    _all_digests = ShardedThreadSafeDict[bytes, int].from_iterable(
        _fst_db_helper.select_all_digests_with_size()
    )

    for _digest, _size in _all_digests.items():
        _rs_f = resource_dir / _digest.hex()

        try:
            assert _rs_f.is_file() and _rs_f.stat().st_size == _size
            assert sha256(_rs_f.read_bytes()).digest() == _digest
        except AssertionError:
            _errs.append(_digest)

    if _errs:
        _err_msg = f"not all resources are collected: {len(_errs)=}"
        logger.error(_err_msg)
        raise AssertionError(_err_msg)
