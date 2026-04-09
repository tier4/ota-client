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
"""Integration tests for CacheDBWriter threads and CacheIndex DB round-trip."""

from __future__ import annotations

import bisect
import random
import sqlite3
import threading
import time
from collections.abc import Callable, Generator
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from ota_proxy.cache_index import CacheDBWriter, CacheIndex
from ota_proxy.config import config as cfg
from ota_proxy.db import CacheMeta, CacheMetaORM, init_db
from ota_proxy.utils import url_based_hash

BUCKET_SIZE_LIST = list(cfg.BUCKET_FILE_SIZE_DICT)
BUCKET_SIZES = [0, 1024, 4096, 256 * 1024, 1024**2, 32 * 1024**2]
DB_ENTRIES = 2_000
ENTRIES_TO_REMOVE = 100


# ---- helpers ---- #


def _db_select_all_sha256_hashes(db_f: Path, table_name: str) -> set[str]:
    with sqlite3.connect(db_f) as con:
        orm = CacheMetaORM(con, table_name)
        return {
            row.file_sha256
            for row in orm.orm_select_entries(_stmt=f"SELECT * FROM {table_name}")
        }


def _db_select_all(db_f: Path, table_name: str) -> dict[str, CacheMeta]:
    with sqlite3.connect(db_f) as con:
        orm = CacheMetaORM(con, table_name)
        return {
            row.file_sha256: row
            for row in orm.orm_select_entries(_stmt=f"SELECT * FROM {table_name}")
        }


def _expected_bucket_idx(cache_size: int) -> int:
    return bisect.bisect_right(BUCKET_SIZE_LIST, cache_size) - 1


def _run_thread(
    target: Callable[[], None], *, close_first: bool, writer: CacheDBWriter
) -> None:
    """Start a CacheDBWriter thread, optionally closing the writer first."""
    if close_first:
        writer.close()
    t = threading.Thread(target=target, daemon=True)
    t.start()
    if not close_first:
        time.sleep(0.3)
        writer.close()
    t.join(timeout=5)


# ---- fixtures ---- #


@pytest.fixture(scope="module")
def entries() -> list[CacheMeta]:
    """Pre-built list of DB_ENTRIES CacheMeta objects with varied cache_size."""
    return [
        CacheMeta(
            file_sha256=url_based_hash(f"http://example.com/f{i}"),
            url=f"http://example.com/f{i}",
            cache_size=BUCKET_SIZES[i % len(BUCKET_SIZES)],
            last_access=int(time.time()),
        )
        for i in range(DB_ENTRIES)
    ]


@pytest.fixture(autouse=True)
def fast_db_cfg(mocker: MockerFixture) -> None:
    """Speed up DB writer loops for all tests in this module."""
    mocker.patch.object(cfg, "DB_WRITER_LOOP_INTERVAL", 0.05)
    mocker.patch.object(cfg, "DB_FLUSH_MAX_LOOPS", 2)


@pytest.fixture
def db_f(tmp_path: Path) -> Path:
    """Initialize an empty cache DB and return the path."""
    _db_f = tmp_path / "cache.db"
    init_db(_db_f, cfg.TABLE_NAME)
    return _db_f


# ---- CacheDBWriter thread tests ---- #


class TestCacheDBWriterDeleteThread:
    """Delete thread drains the queue and flushes deletes to DB."""

    @pytest.fixture
    def db_writer(
        self, db_f: Path, entries: list[CacheMeta]
    ) -> Generator[tuple[CacheDBWriter, Path, list[CacheMeta]]]:
        with sqlite3.connect(db_f) as con:
            orm = CacheMetaORM(con)
            orm.orm_insert_entries(entries, or_option="replace")

        writer = CacheDBWriter(db_f)
        yield writer, db_f, entries

    @pytest.mark.parametrize(
        "close_first", [False, True], ids=["during_loop", "on_close"]
    )
    def test_delete_thread_flushes_queue(
        self,
        db_writer: tuple[CacheDBWriter, Path, list[CacheMeta]],
        close_first: bool,
    ) -> None:
        """Enqueued deletes are flushed both during the loop and on shutdown."""
        writer, db_f, entries = db_writer

        to_delete = random.sample(entries, ENTRIES_TO_REMOVE)
        to_delete_keys = {e.file_sha256 for e in to_delete}
        for e in to_delete:
            writer.remove_entry(e.file_sha256)

        _run_thread(writer.start_delete_thread, close_first=close_first, writer=writer)

        remaining = _db_select_all_sha256_hashes(db_f, cfg.TABLE_NAME)
        for key in to_delete_keys:
            assert key not in remaining
        assert len(remaining) == DB_ENTRIES - ENTRIES_TO_REMOVE


class TestCacheDBWriterWriteThread:
    """Write thread drains the queue and flushes writes to DB."""

    @pytest.fixture
    def db_writer(
        self, db_f: Path
    ) -> Generator[tuple[CacheDBWriter, Path], None, None]:
        writer = CacheDBWriter(db_f)
        yield writer, db_f

    @pytest.mark.parametrize(
        "close_first", [False, True], ids=["during_loop", "on_close"]
    )
    def test_write_thread_flushes_queue(
        self,
        db_writer: tuple[CacheDBWriter, Path],
        entries: list[CacheMeta],
        close_first: bool,
    ) -> None:
        """Enqueued writes are flushed both during the loop and on shutdown."""
        writer, db_f = db_writer

        for e in entries:
            writer.register_entry(e)

        _run_thread(writer.start_write_thread, close_first=close_first, writer=writer)

        persisted = _db_select_all_sha256_hashes(db_f, cfg.TABLE_NAME)
        assert len(persisted) == DB_ENTRIES
        for e in entries:
            assert e.file_sha256 in persisted

    def test_write_thread_assigns_bucket_idx(
        self,
        db_writer: tuple[CacheDBWriter, Path],
        entries: list[CacheMeta],
    ) -> None:
        """Write thread must assign correct bucket_idx for backward compat."""
        writer, db_f = db_writer

        for e in entries:
            writer.register_entry(
                CacheMeta(
                    file_sha256=e.file_sha256,
                    url=e.url,
                    last_access=e.last_access,
                    cache_size=e.cache_size,
                    content_encoding=e.content_encoding,
                    file_compression_alg=e.file_compression_alg,
                    # NOTE: intentionally NOT include the bucket_id,
                    #       test the writer thread set the bucket_id correctly.
                )
            )

        _run_thread(writer.start_write_thread, close_first=False, writer=writer)

        rows = _db_select_all(db_f, cfg.TABLE_NAME)
        for e in entries:
            assert rows[e.file_sha256].bucket_idx == _expected_bucket_idx(e.cache_size)


# ---- CacheIndex full round-trip ---- #


class TestCacheIndexCommitAndRemove:
    """CacheIndex commit_entry / remove_entry with real DB threads."""

    @pytest.fixture
    def cache_index(
        self, db_f: Path, tmp_path: Path
    ) -> Generator[tuple[CacheIndex, Path, Path], None, None]:
        base_dir = tmp_path / "ota-cache"
        base_dir.mkdir()
        idx = CacheIndex(db_f, base_dir, init_db=True)
        try:
            yield idx, db_f, base_dir
        finally:
            idx.close()

    def test_committed_entries_persisted_to_db(
        self,
        cache_index: tuple[CacheIndex, Path, Path],
        entries: list[CacheMeta],
    ) -> None:
        """commit_entry must land in both in-memory index and DB."""
        idx, db_f, base_dir = cache_index

        for e in entries:
            (base_dir / e.file_sha256).touch()
            idx.commit_entry(e)

        # In-memory lookups work immediately
        for e in entries:
            assert idx.lookup_entry(e.file_sha256) is not None

        # Flush DB and verify persistence + bucket_idx
        idx.close()
        rows = _db_select_all(db_f, cfg.TABLE_NAME)
        for e in entries:
            assert e.file_sha256 in rows
            assert rows[e.file_sha256].bucket_idx == _expected_bucket_idx(e.cache_size)

    def test_remove_entry_clears_index_and_db(
        self, db_f: Path, tmp_path: Path, entries: list[CacheMeta]
    ) -> None:
        """remove_entry must drop from in-memory index and DB."""
        base_dir = tmp_path / "ota-cache-rm"
        base_dir.mkdir()

        # Phase 1: commit entries and flush writes completely.
        idx = CacheIndex(db_f, base_dir, init_db=True)
        for e in entries:
            (base_dir / e.file_sha256).touch()
            idx.commit_entry(e)
        idx.close()

        assert len(_db_select_all_sha256_hashes(db_f, cfg.TABLE_NAME)) == DB_ENTRIES

        # Phase 2: reopen and remove a random subset.
        idx = CacheIndex(db_f, base_dir)
        to_remove = random.sample(entries, ENTRIES_TO_REMOVE)
        to_remove_keys = {e.file_sha256 for e in to_remove}
        for e in to_remove:
            idx.remove_entry(e.file_sha256)

        # In-memory index reflects removals immediately
        for e in to_remove:
            assert idx.lookup_entry(e.file_sha256) is None
        for e in entries:
            if e.file_sha256 not in to_remove_keys:
                assert idx.lookup_entry(e.file_sha256) is not None

        # Flush and verify DB
        idx.close()
        remaining = _db_select_all_sha256_hashes(db_f, cfg.TABLE_NAME)
        for key in to_remove_keys:
            assert key not in remaining
        assert len(remaining) == DB_ENTRIES - ENTRIES_TO_REMOVE
