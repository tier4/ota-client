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
import shutil
import sqlite3
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from typing import NamedTuple

import pytest

from ota_metadata.file_table.db import (
    FileTableDBHelper,
    FileTableDirORM,
    FileTableInodeORM,
    FileTableNonRegularORM,
    FileTableRegularORM,
    FileTableResourceORM,
)
from ota_metadata.legacy2.csv_parser import (
    parse_dirs_from_csv_file,
    parse_regulars_from_csv_file,
    parse_symlinks_from_csv_file,
)
from ota_metadata.legacy2.rs_table import ResourceTableORM
from otaclient_common.thread_safe_container import ShardedThreadSafeDict
from tests.conftest import OTA_IMAGE_DIR

REGULARS_TXT = OTA_IMAGE_DIR / "regulars.txt"
DIRS_TXT = OTA_IMAGE_DIR / "dirs.txt"
SYMLINKS_TXT = OTA_IMAGE_DIR / "symlinks.txt"

OTA_IMAGE_DATA_DIR = OTA_IMAGE_DIR / "data"

logger = logging.getLogger(__name__)


# NOTE: during delta calculation, the ft_resource table will be altered, so
#       this fixture needs to be function scope fixture.
@pytest.fixture
def fst_db_helper(
    tmp_path_factory: pytest.TempPathFactory,
) -> FileTableDBHelper:
    """
    Referring to metadata._prepare_ota_image_metadata method.
    """
    image_meta_d = tmp_path_factory.mktemp(basename="image-meta")
    ft_dbf = image_meta_d / "file_table.sqlite3"
    rst_dbf = image_meta_d / "resource_table.sqlite3"

    with closing(sqlite3.connect(ft_dbf)) as fst_conn, closing(
        sqlite3.connect(rst_dbf)
    ) as rst_conn:
        # ------ bootstrap each tables in the file_table database ------ #
        ft_regular_orm = FileTableRegularORM(fst_conn)
        ft_regular_orm.orm_bootstrap_db()
        ft_dir_orm = FileTableDirORM(fst_conn)
        ft_dir_orm.orm_bootstrap_db()
        ft_non_regular_orm = FileTableNonRegularORM(fst_conn)
        ft_non_regular_orm.orm_bootstrap_db()
        ft_resource_orm = FileTableResourceORM(fst_conn)
        ft_resource_orm.orm_bootstrap_db()
        ft_inode_orm = FileTableInodeORM(fst_conn)
        ft_inode_orm.orm_bootstrap_db()
        rs_orm = ResourceTableORM(rst_conn)
        rs_orm.orm_bootstrap_db()

        inode_start = 1
        regulars_num, inode_start = parse_regulars_from_csv_file(
            _fpath=REGULARS_TXT,
            _orm=ft_regular_orm,
            _orm_ft_resource=ft_resource_orm,
            _orm_rs=rs_orm,
            _orm_inode=ft_inode_orm,
            inode_start=inode_start,
        )

        dirs_num, inode_start = parse_dirs_from_csv_file(
            DIRS_TXT,
            ft_dir_orm,
            _inode_orm=ft_inode_orm,
            inode_start=inode_start,
        )

        symlinks_num, _ = parse_symlinks_from_csv_file(
            SYMLINKS_TXT,
            ft_non_regular_orm,
            _inode_orm=ft_inode_orm,
            inode_start=inode_start,
        )
        logger.info(
            f"csv parse finished: {dirs_num=}, {symlinks_num=}, {regulars_num=}"
        )
    return FileTableDBHelper(ft_dbf)


class SlotAB(NamedTuple):
    slot_a: Path
    slot_b: Path


@pytest.fixture
def ab_slots_for_inplace(tmp_path: Path) -> SlotAB:
    logger.info("prepare simple a/b slots for inplace update mode ...")
    slot_a = tmp_path / "slot_a"
    slot_b = tmp_path / "slot_b"
    logger.info(f"prepare simple a/b slots for inplace mode: {slot_a=}, {slot_b=}")

    slot_a.mkdir(exist_ok=True, parents=True)
    shutil.copytree(OTA_IMAGE_DATA_DIR, slot_b, symlinks=True)

    # NOTE(20250702): edge condition found on bench test:
    #   fpath of a folder in standby slot becomes symlink in the new image.
    # in newer ubuntu, /sbin becomes a symlink points to /usr/sbin
    sbin = slot_b / "sbin"
    sbin.unlink(missing_ok=True)
    sbin.mkdir(exist_ok=True, parents=True)
    return SlotAB(slot_a, slot_b)


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


@pytest.fixture
def resource_dir(tmp_path: Path):
    _rd = tmp_path / ".ota-tmp"
    logger.info(f"prepare function scrope resource dir: {_rd} ...")
    try:
        _rd.mkdir()
        yield _rd
    finally:
        logger.info(f"cleanup function scrope resource dir: {_rd} ...")
        shutil.rmtree(_rd, ignore_errors=True)
