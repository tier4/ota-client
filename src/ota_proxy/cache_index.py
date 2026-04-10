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

import bisect
import logging
import os
import queue
import sqlite3
import sys
import threading
import time
import typing
from pathlib import Path
from typing import Iterable, NamedTuple

from multidict import CIMultiDict
from simple_sqlite3_orm import ORMBase, gen_sql_stmt
from simple_sqlite3_orm.utils import enable_wal_mode

from otaclient_common._logging import get_burst_suppressed_logger
from otaclient_common._typing import StrOrPath

from ._consts import HEADER_CONTENT_ENCODING, HEADER_OTA_FILE_CACHE_CONTROL
from .cache_control_header import export_kwargs_as_header_string
from .config import config as cfg
from .db import CacheMeta, CacheMetaORM, check_db, init_db
from .utils import batched

logger = logging.getLogger(__name__)
burst_suppressed_logger = get_burst_suppressed_logger(f"{__name__}.db_error")

PRELOAD_BATCH_REMOVE = 128
DB_SHUTDOWN_TIMEOUT = 10
"""Timeout for waiting the DB writer threads to
flush the pending commits."""


class CacheIndexEntry(NamedTuple):
    cache_size: int
    file_compression_alg: str | None
    content_encoding: str | None

    def export_headers(self, file_sha256: str) -> CIMultiDict[str]:
        res: CIMultiDict[str] = CIMultiDict()
        if self.content_encoding:
            res[HEADER_CONTENT_ENCODING] = self.content_encoding

        if file_sha256 and not file_sha256.startswith(cfg.URL_BASED_HASH_PREFIX):
            res[HEADER_OTA_FILE_CACHE_CONTROL] = export_kwargs_as_header_string(
                file_sha256=file_sha256,
                file_compression_alg=self.file_compression_alg or "",
            )
        return res


_WRITER_STOP_SENTINEL = typing.cast("CacheMeta", object())
_DELETER_STOP_SENTINEL = typing.cast("str", object())


class CacheDBWriter:
    """Dedicated threads that batch CacheMeta writes and deletes to SQLite."""

    def __init__(self, db_f: Path, orm_type: type[ORMBase] = CacheMetaORM) -> None:
        self._db_f = db_f
        self._orm_type = orm_type
        self._write_queue: queue.Queue[CacheMeta] = queue.Queue()
        self._delete_queue: queue.Queue[str] = queue.Queue()

        _write_con = sqlite3.connect(self._db_f, check_same_thread=False)
        enable_wal_mode(_write_con)
        self._write_orm = orm_type(_write_con)

        _delete_con = sqlite3.connect(self._db_f, check_same_thread=False)
        enable_wal_mode(_delete_con)
        self._delete_orm = orm_type(_delete_con)

        self._bucket_size_list = list(cfg.BUCKET_FILE_SIZE_DICT)

    def register_entry(self, entry: CacheMeta) -> None:
        """Enqueue an entry for batched DB write (non-blocking)."""
        self._write_queue.put_nowait(entry)

    def remove_entry(self, file_sha256: str) -> None:
        """Enqueue a key for batched DB deletion (non-blocking)."""
        self._delete_queue.put_nowait(file_sha256)

    def close(self) -> None:
        self._write_queue.put_nowait(_WRITER_STOP_SENTINEL)
        self._delete_queue.put_nowait(_DELETER_STOP_SENTINEL)

    def start_write_thread(self) -> None:
        batch: list[CacheMeta] = []
        try:
            while True:
                _wait_timeout = False
                try:
                    _item = self._write_queue.get(timeout=cfg.DB_WRITER_LOOP_INTERVAL)
                    if _item is _WRITER_STOP_SENTINEL:
                        self._flush_writes(batch)
                        return batch.clear()
                    batch.append(_item)
                except queue.Empty:
                    _wait_timeout = True

                if len(batch) >= cfg.DB_FLUSH_BATCH_SIZE or (batch and _wait_timeout):
                    self._flush_writes(batch)
                    batch.clear()
        finally:
            self._write_orm.orm_con.close()

    def start_delete_thread(self) -> None:
        batch: list[str] = []
        try:
            while True:
                _wait_timeout = False
                try:
                    _item = self._delete_queue.get(timeout=cfg.DB_WRITER_LOOP_INTERVAL)
                    if _item is _DELETER_STOP_SENTINEL:
                        self._flush_deletes(batch)
                        return batch.clear()
                    batch.append(_item)
                except queue.Empty:
                    _wait_timeout = True

                if len(batch) >= cfg.DB_FLUSH_BATCH_SIZE or (batch and _wait_timeout):
                    self._flush_deletes(batch)
                    batch.clear()
        finally:
            self._delete_orm.orm_con.close()

    def _flush_writes(self, _batch: Iterable[CacheMeta]) -> None:
        """Write a batch of entries to SQLite in one transaction."""
        # Fill bucket_idx for backward compat with older otaproxy LRU
        for entry in _batch:
            entry.bucket_idx = (
                bisect.bisect_right(self._bucket_size_list, entry.cache_size) - 1
            )

        for _sub_batch in batched(_batch, cfg.DB_FLUSH_BATCH_SIZE):
            try:
                self._write_orm.orm_insert_entries(_sub_batch, or_option="replace")
            except Exception as e:
                burst_suppressed_logger.exception(
                    f"cache index: failed to flush writes to DB: {e!r}"
                )

    def _flush_deletes(self, _batch: list[str]) -> None:
        """Delete a batch of entries from SQLite in one transaction."""
        for _sub_batch in batched(_batch, cfg.DB_FLUSH_BATCH_SIZE):
            try:
                # fmt: off
                self._delete_orm.orm_execute(
                    gen_sql_stmt(
                        "DELETE", "FROM", self._delete_orm.orm_table_name,
                        "WHERE", "file_sha256", "IN", f"({','.join('?' for _ in _sub_batch)})"
                    ),
                    tuple(_sub_batch),
                )
                # fmt: on
            except Exception as e:
                burst_suppressed_logger.exception(
                    f"cache index: failed to flush deletes to DB: {e!r}"
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
        Path(base_dir).mkdir(0o700, exist_ok=True, parents=True)
        Path(db_f).parent.mkdir(0o700, exist_ok=True, parents=True)

        self._db_f = Path(db_f)
        self._base_dir = str(base_dir)
        self._table_name = table_name
        self._entries_exceeded_warned = False

        self._index: dict[str, CacheIndexEntry] = {}

        if init_db:
            logger.info("cache DB init requested ...")
            self._force_init_db()
        else:
            # Validate DB and load, re-init on failure
            self._ensure_db_and_load()

        self._db_writer = _db_writer = CacheDBWriter(self._db_f)
        self._db_write_thread = threading.Thread(
            target=_db_writer.start_write_thread,
            daemon=True,
            name="cache_index_db_writer",
        )
        self._db_delete_thread = threading.Thread(
            target=_db_writer.start_delete_thread,
            daemon=True,
            name="cache_index_db_deleter",
        )
        self._db_write_thread.start()
        self._db_delete_thread.start()

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
            return

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
                    "WHERE", "file_sha256", "IN", f"({_place_holders})"
                ),
                _batch
            )
            # fmt: on

    def _preload_from_db(self) -> tuple[dict[str, CacheIndexEntry], list[str]]:
        _res, _to_remove = {}, []
        with sqlite3.connect(self._db_f) as con:
            orm = CacheMetaORM(con, self._table_name)
            _count = 0
            # fmt: off
            for row in orm.orm_select_entries(
                _stmt=gen_sql_stmt(
                    "SELECT", "*",
                    "FROM", self._table_name,
                    "ORDER BY", "last_access", "DESC"
                )
            ):
            # fmt: on
                if _count < cfg.MAX_INDEX_ENTRIES:
                    _key = sys.intern(row.file_sha256)
                    if not os.path.exists(os.path.join(self._base_dir, _key)):
                        _to_remove.append(_key)
                        continue

                    _res[_key] = CacheIndexEntry(
                        cache_size=row.cache_size,
                        file_compression_alg=sys.intern(row.file_compression_alg)
                        if row.file_compression_alg
                        else None,
                        content_encoding=sys.intern(row.content_encoding)
                        if row.content_encoding
                        else None,
                    )
                    _count += 1
                else:
                    _to_remove.append(row.file_sha256)
        return _res, _to_remove

    def _preload_cleanup_cache_files(self, _exceed: list[str]) -> None:
        for h in _exceed:
            try:
                os.unlink(os.path.join(self._base_dir, h))
            except FileNotFoundError:
                pass

    def _preload_cleanup_cache_db_entries(self, _exceed: list[str]) -> None:
        with sqlite3.connect(self._db_f) as con:
            for _batch in batched(_exceed, PRELOAD_BATCH_REMOVE):
                self._batch_remove_entries(con, self._table_name, list(_batch))

    def lookup_entry(self, file_sha256: str) -> CacheIndexEntry | None:
        # with GIL, read from dict is atomic
        return self._index.get(file_sha256)

    def remove_entry(self, file_sha256: str) -> None:
        """Remove entry from in-memory index and register remove from DB."""
        self._index.pop(file_sha256, None)
        self._db_writer.remove_entry(file_sha256)

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
                    "will drop future cache index register"
                )
            return False

        # with GIL, write to dict is atomic
        self._index[key] = index_entry
        self._db_writer.register_entry(entry)
        return True

    def close(self) -> None:
        self._db_writer.close()
        self._db_write_thread.join(timeout=DB_SHUTDOWN_TIMEOUT)
        self._db_delete_thread.join(timeout=DB_SHUTDOWN_TIMEOUT)
