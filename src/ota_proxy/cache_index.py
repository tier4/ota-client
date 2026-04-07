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
"""In-memory cache index with batched DB write-behind."""

from __future__ import annotations

import logging
import os
import queue
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import NamedTuple

from multidict import CIMultiDict
from simple_sqlite3_orm import ORMBase, gen_sql_stmt

from otaclient_common._logging import get_burst_suppressed_logger
from otaclient_common._typing import StrOrPath

from ._consts import HEADER_CONTENT_ENCODING, HEADER_OTA_FILE_CACHE_CONTROL
from .cache_control_header import export_kwargs_as_header_string
from .config import config as cfg
from .db import CacheMeta, CacheMetaORM, check_db, init_db

logger = logging.getLogger(__name__)
burst_suppressed_logger = get_burst_suppressed_logger(f"{__name__}.db_error")

PRELOAD_BATCH_REMOVE = 128
DB_SHUTDOWN_TIMEOUT = 10  # seconds


def export_headers(file_sha256: str, entry: CacheIndexEntry) -> CIMultiDict[str]:
    res: CIMultiDict[str] = CIMultiDict()
    if entry.content_encoding:
        res[HEADER_CONTENT_ENCODING] = entry.content_encoding

    if file_sha256 and not file_sha256.startswith(cfg.URL_BASED_HASH_PREFIX):
        res[HEADER_OTA_FILE_CACHE_CONTROL] = export_kwargs_as_header_string(
            file_sha256=file_sha256,
            file_compression_alg=entry.file_compression_alg or "",
        )
    return res


class CacheIndexEntry(NamedTuple):
    cache_size: int
    file_compression_alg: str | None
    content_encoding: str | None


class CacheDBWriter:
    """Dedicated thread that batches CacheMeta writes to SQLite."""

    def __init__(self, db_f: Path, orm_type: type[ORMBase] = CacheMetaORM) -> None:
        self._db_f = db_f
        self._orm_type = orm_type
        self._queue: queue.Queue[CacheMeta] = queue.Queue()
        self._orm = orm_type(sqlite3.connect(self._db_f, check_same_thread=False))

        self._closed = False

    def register_entry(self, entry: CacheMeta) -> None:
        """Enqueue an entry for batched DB write (non-blocking)."""
        self._queue.put_nowait(entry)

    def close(self) -> None:
        self._closed = True

    def start(self) -> None:
        batch: list[CacheMeta] = []
        loops_since_flush = 0
        while not self._closed:
            time.sleep(cfg.DB_WRITER_LOOP_INTERVAL)

            # Drain queue up to batch size
            while len(batch) < cfg.DB_FLUSH_BATCH_SIZE:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break

            loops_since_flush += 1

            # Flush when batch is full or enough loops passed with pending entries
            if len(batch) >= cfg.DB_FLUSH_BATCH_SIZE or (
                loops_since_flush >= cfg.DB_FLUSH_MAX_LOOPS and batch
            ):
                self._flush(batch)
                batch.clear()
                loops_since_flush = 0

        if batch:
            self._flush(batch)

        self._orm._con.close()

    def _flush(self, _batch: list[CacheMeta]) -> None:
        """Write a batch of entries to SQLite in one transaction."""
        try:
            self._orm.orm_insert_entries(_batch, or_option="replace")
        except Exception as e:
            burst_suppressed_logger.exception(
                f"cache index: failed to flush {len(_batch)} entries to DB: {e!r}"
            )


class CacheIndex:
    """In-memory cache index backed by SQLite for persistence."""

    def __init__(
        self,
        db_f: StrOrPath,
        base_dir: StrOrPath,
        *,
        init_db: bool = False,
        table_name: str = cfg.TABLE_NAME,
    ):
        self._db_f = Path(db_f)
        self._base_dir = str(base_dir)
        self._table_name = table_name
        self._entries_exceeded_warned = False

        self._index: dict[str, CacheIndexEntry] = {}

        if init_db:
            self._force_init_db()
        else:
            # Validate DB and load, re-init on failure
            self._ensure_db_and_load()

        self._db_writer = _db_writer = CacheDBWriter(self._db_f)
        self._db_writer_thread = threading.Thread(
            target=_db_writer.start,
            daemon=True,
            name="cache_index_db_writer",
        )
        self._db_writer_thread.start()

    def _force_init_db(self) -> None:
        """Delete and re-create the DB file with an empty table."""
        logger.info("force init cache DB ...")
        self._db_f.unlink(missing_ok=True)
        init_db(self._db_f, self._table_name)
        logger.info(f"cache index: re-initialized DB at {self._db_f}")

    def _ensure_db_and_load(self) -> None:
        """Validate DB, load entries into index. Re-init DB on any failure."""
        if not check_db(self._db_f, self._table_name):
            logger.warning(
                f"cache index: DB validation failed, re-initializing {self._db_f}"
            )
            self._force_init_db()
            return  # fresh DB, nothing to load

        try:
            _res, _exceed = self._preload_from_db()
            self._index.update(_res)
            logger.info(f"cache index: pre-load {len(_res)} entries from DB")
            if _exceed:
                logger.info(f"cache index: clean up exceeded entries {len(_exceed)=}")
                self._preload_cleanup_cache_files(_exceed)
                self._preload_cleanup_cache_db_entries(_exceed)

            logger.info("pre-load DB finished")
        except Exception as e:
            logger.exception(
                f"cache index: failed to load from DB, re-initializing {self._db_f}: {e!r}"
            )

            self._index.clear()
            self._force_init_db()

    @staticmethod
    def _batch_remove_entries(
        con: sqlite3.Connection, _table_name: str, _batch: list[str]
    ) -> None:
        _place_holders = ",".join("?" for _ in _batch)
        with con:
            # fmt: off
            con.execute(
                gen_sql_stmt(
                    "DELETE", "FROM", _table_name,
                    "WHERE", "file_sha256", "IN", _place_holders
                ),
                _batch
            )
            # fmt: on

    def _preload_from_db(self) -> tuple[dict[str, CacheIndexEntry], list[str]]:
        _res, _exceed = {}, []
        with sqlite3.connect(self._db_f) as con:
            orm = CacheMetaORM(con, self._table_name)
            # fmt: off
            for _count, row in enumerate(
                orm.orm_select_entries(
                    _stmt=gen_sql_stmt(
                        "SELECT", "*",
                        "FROM", self._table_name,
                        "ORDER BY", "last_access", "DESC"
                    )
                ), start=1,
            ):
            # fmt: on
                if _count <= cfg.MAX_INDEX_ENTRIES:
                    _res[sys.intern(row.file_sha256)] = CacheIndexEntry(
                        cache_size=row.cache_size,
                        file_compression_alg=sys.intern(row.file_compression_alg)
                        if row.file_compression_alg
                        else None,
                        content_encoding=sys.intern(row.content_encoding)
                        if row.content_encoding
                        else None,
                    )
                else:
                    _exceed.append(row.file_sha256)

        return _res, _exceed

    def _preload_cleanup_cache_files(self, _exceed: list[str]) -> None:
        for h in _exceed:
            try:
                os.unlink(os.path.join(self._base_dir, h))
            except FileNotFoundError:
                pass

    def _preload_cleanup_cache_db_entries(self, _exceed: list[str]) -> None:
        with sqlite3.connect(self._db_f) as con:
            _batch_hashes = []
            for _count, _entry in enumerate(_exceed, start=1):
                _batch_hashes.append(_entry)
                if _count % PRELOAD_BATCH_REMOVE == 0:
                    self._batch_remove_entries(con, self._table_name, _batch_hashes)
                    _batch_hashes.clear()

            if _batch_hashes:
                self._batch_remove_entries(con, self._table_name, _batch_hashes)

    def lookup_entry(self, file_sha256: str) -> CacheIndexEntry | None:
        # with GIL, read from dict is atomic
        return self._index.get(file_sha256)

    def remove_entry(self, file_sha256: str) -> None:
        """Remove entry from in-memory index and DB."""
        self._index.pop(file_sha256, None)

    def commit_entry(self, entry: CacheMeta) -> bool:
        """Add entry to in-memory index and queue DB write."""
        entry.last_access = int(time.time())

        key = sys.intern(entry.file_sha256)
        index_entry = CacheIndexEntry(
            cache_size=entry.cache_size,
            file_compression_alg=sys.intern(entry.file_compression_alg)
            if entry.file_compression_alg
            else None,
            content_encoding=sys.intern(entry.content_encoding)
            if entry.content_encoding
            else None,
        )

        if len(self._index) >= cfg.MAX_INDEX_ENTRIES:
            if not self._entries_exceeded_warned:
                self._entries_exceeded_warned = True
                logger.warning(
                    f"cache index entries num exceeds {cfg.MAX_INDEX_ENTRIES}, "
                    "will drop future  cache index register"
                )
            return False

        # with GIL, write to dict is atomic
        self._index[key] = index_entry
        self._db_writer.register_entry(entry)
        return True

    def close(self) -> None:
        self._db_writer.close()
        self._db_writer_thread.join(timeout=DB_SHUTDOWN_TIMEOUT)
