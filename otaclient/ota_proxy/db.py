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

import asyncio
import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ._consts import HEADER_CONTENT_ENCODING, HEADER_OTA_FILE_CACHE_CONTROL
from .cache_control import OTAFileCacheControl
from .config import config as cfg
from .orm import FV, ColumnDescriptor, ORMBase

logger = logging.getLogger(__name__)


class CacheMeta(ORMBase):
    """revision 4

    url: unquoted URL from the request of this cache entry.
    bucket_id: the LRU bucket this cache entry in.
    last_access: the UNIX timestamp of last access.
    cache_size: the file size of cached file(not the size of corresponding OTA file!).
    file_sha256:
        a. string of the sha256 hash of original OTA file(uncompressed),
        b. if a. is not available, then it is in form of "URL_<sha256_of_URL>".
    file_compression_alg: the compression used for the cached OTA file entry,
        if no compression is applied, then empty.
    content_encoding: the content_encoding header string comes with resp from remote server.
    """

    file_sha256: ColumnDescriptor[str] = ColumnDescriptor(
        str, "TEXT", "UNIQUE", "NOT NULL", "PRIMARY KEY"
    )
    url: ColumnDescriptor[str] = ColumnDescriptor(str, "TEXT", "NOT NULL")
    bucket_idx: ColumnDescriptor[int] = ColumnDescriptor(
        int, "INTEGER", "NOT NULL", type_guard=True
    )
    last_access: ColumnDescriptor[int] = ColumnDescriptor(
        int, "INTEGER", "NOT NULL", type_guard=(int, float)
    )
    cache_size: ColumnDescriptor[int] = ColumnDescriptor(
        int, "INTEGER", "NOT NULL", type_guard=True
    )
    file_compression_alg: ColumnDescriptor[str] = ColumnDescriptor(
        str, "TEXT", "NOT NULL"
    )
    content_encoding: ColumnDescriptor[str] = ColumnDescriptor(str, "TEXT", "NOT NULL")

    def export_headers_to_client(self) -> Dict[str, str]:
        """Export required headers for client.

        Currently includes content-type, content-encoding and ota-file-cache-control headers.
        """
        res = {}
        if self.content_encoding:
            res[HEADER_CONTENT_ENCODING] = self.content_encoding

        # export ota-file-cache-control headers if file_sha256 is valid file hash
        if self.file_sha256 and not self.file_sha256.startswith(
            cfg.URL_BASED_HASH_PREFIX
        ):
            res[HEADER_OTA_FILE_CACHE_CONTROL] = (
                OTAFileCacheControl.export_kwargs_as_header(
                    file_sha256=self.file_sha256,
                    file_compression_alg=self.file_compression_alg,
                )
            )
        return res


class OTACacheDB:
    TABLE_NAME: str = cfg.TABLE_NAME
    OTA_CACHE_IDX: List[str] = [
        (
            "CREATE INDEX IF NOT EXISTS "
            f"bucket_last_access_idx_{TABLE_NAME} "
            f"ON {TABLE_NAME}({CacheMeta.bucket_idx.name}, {CacheMeta.last_access.name})"
        ),
    ]

    @classmethod
    def check_db_file(cls, db_file: Union[str, Path]) -> bool:
        if not Path(db_file).is_file():
            return False
        try:
            with sqlite3.connect(db_file) as con:
                con.execute("PRAGMA integrity_check;")
                # check whether expected table is in it or not
                cur = con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (cls.TABLE_NAME,),
                )
                if cur.fetchone() is None:
                    logger.warning(
                        f"{cls.TABLE_NAME} not found, this db file should be initialized"
                    )
                    return False
                return True
        except sqlite3.DatabaseError as e:
            logger.warning(f"{db_file} is corrupted: {e!r}")
            return False

    @classmethod
    def init_db_file(cls, db_file: Union[str, Path]):
        """
        Purge the old db file and init the db files, creating table in it.
        """
        # remove the db file first
        Path(db_file).unlink(missing_ok=True)
        try:
            with sqlite3.connect(db_file) as con:
                # create ota_cache table
                con.execute(CacheMeta.get_create_table_stmt(cls.TABLE_NAME), ())
                # create indices
                for idx in cls.OTA_CACHE_IDX:
                    con.execute(idx, ())

                ### db performance tunning
                # enable WAL mode
                con.execute("PRAGMA journal_mode = WAL;")
                # set temp_store to memory
                con.execute("PRAGMA temp_store = memory;")
                # enable mmap (size in bytes)
                mmap_size = 16 * 1024 * 1024  # 16MiB
                con.execute(f"PRAGMA mmap_size = {mmap_size};")
        except sqlite3.Error as e:
            logger.debug(f"init db failed: {e!r}")
            raise e

    def __init__(self, db_file: Union[str, Path]):
        """Connects to OTA cache database.

        Args:
            db_file: target db file to connect to.

        Raises:
            ValueError on invalid ota_cache db file,
            sqlite3.Error if optimization settings applied failed.
        """
        self._con = sqlite3.connect(
            db_file,
            check_same_thread=True,  # one thread per connection in the threadpool
            # isolation_level=None,  # enable autocommit mode
        )
        self._con.row_factory = sqlite3.Row
        if not self.check_db_file(db_file):
            raise ValueError(f"invalid db file: {db_file}")

        # db performance tunning, enable optimization
        with self._con as con:
            # enable WAL mode
            con.execute("PRAGMA journal_mode = WAL;")
            # set temp_store to memory
            con.execute("PRAGMA temp_store = memory;")
            # enable mmap (size in bytes)
            mmap_size = 16 * 1024 * 1024  # 16MiB
            con.execute(f"PRAGMA mmap_size = {mmap_size};")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def close(self):
        self._con.close()

    def remove_entries(self, fd: ColumnDescriptor[FV], *_inputs: FV) -> int:
        """Remove entri(es) indicated by field(s).

        Args:
            fd: field descriptor of the column
            *_inputs: a list of field value(s)

        Returns:
            returns affected rows count.
        """
        if not _inputs:
            return 0
        if CacheMeta.contains_field(fd):
            with self._con as con:
                _regulated_input = [(i,) for i in _inputs]
                return con.executemany(
                    f"DELETE FROM {self.TABLE_NAME} WHERE {fd._field_name}=?",
                    _regulated_input,
                ).rowcount
        else:
            logger.debug(f"invalid inputs detected: {_inputs=}")
            return 0

    def lookup_entry(
        self,
        fd: ColumnDescriptor[FV],
        value: FV,
    ) -> Optional[CacheMeta]:
        """Lookup entry by field value.

        NOTE: lookup via this method will trigger update to <last_access> field,
        NOTE 2: <last_access> field is updated by searching <fd> key.

        Args:
            fd: field descriptor of the column.
            value: field value to lookup.

        Returns:
            An instance of CacheMeta representing the cache entry, or None if lookup failed.
        """
        if not CacheMeta.contains_field(fd):
            return

        fd_name = fd.name
        with self._con as con:  # put the lookup and update into one session
            if row := con.execute(
                f"SELECT * FROM {self.TABLE_NAME} WHERE {fd_name}=?",
                (value,),
            ).fetchone():
                # warm up the cache(update last_access timestamp) here
                res = CacheMeta.row_to_meta(row)
                con.execute(
                    (
                        f"UPDATE {self.TABLE_NAME} SET {CacheMeta.last_access.name}=? "
                        f"WHERE {fd_name}=?"
                    ),
                    (int(time.time()), value),
                )
                return res

    def insert_entry(self, *cache_meta: CacheMeta) -> int:
        """Insert an entry into the ota cache table.

        Args:
            cache_meta: a list of CacheMeta instances to be inserted

        Returns:
            Returns inserted rows count.
        """
        if not cache_meta:
            return 0
        with self._con as con:
            return con.executemany(
                f"INSERT OR REPLACE INTO {self.TABLE_NAME} VALUES ({CacheMeta.get_shape()})",
                [m.astuple() for m in cache_meta],
            ).rowcount

    def lookup_all(self) -> List[CacheMeta]:
        """Lookup all entries in the ota cache table.

        Returns:
            A list of CacheMeta instances representing each entry.
        """
        with self._con as con:
            cur = con.execute(f"SELECT * FROM {self.TABLE_NAME}", ())
            return [CacheMeta.row_to_meta(row) for row in cur.fetchall()]

    def rotate_cache(self, bucket_idx: int, num: int) -> Optional[List[str]]:
        """Rotate cache entries in LRU flavour.

        Args:
            bucket_idx: which bucket for space reserving
            num: num of entries needed to be deleted in this bucket

        Return:
            A list of OTA file's hashes that needed to be deleted for space reserving,
                or None if no enough entries for space reserving.
        """
        bucket_fn, last_access_fn = (
            CacheMeta.bucket_idx.name,
            CacheMeta.last_access.name,
        )
        # first, check whether we have required number of entries in the bucket
        with self._con as con:
            cur = con.execute(
                (
                    f"SELECT COUNT(*) FROM {self.TABLE_NAME} WHERE {bucket_fn}=? "
                    f"ORDER BY {last_access_fn} LIMIT ?"
                ),
                (bucket_idx, num),
            )
            if not (_raw_res := cur.fetchone()):
                return

            # NOTE: if we can upgrade to sqlite3 >= 3.35,
            # use RETURNING clause instead of using 2 queries as below

            # if we have enough entries for space reserving
            if _raw_res[0] >= num:
                # first select those entries
                _rows = con.execute(
                    (
                        f"SELECT * FROM {self.TABLE_NAME} "
                        f"WHERE {bucket_fn}=? "
                        f"ORDER BY {last_access_fn} "
                        "LIMIT ?"
                    ),
                    (bucket_idx, num),
                ).fetchall()
                # and then delete those entries with same conditions
                con.execute(
                    (
                        f"DELETE FROM {self.TABLE_NAME} "
                        f"WHERE {bucket_fn}=? "
                        f"ORDER BY {last_access_fn} "
                        "LIMIT ?"
                    ),
                    (bucket_idx, num),
                )
                return [row[CacheMeta.file_sha256.name] for row in _rows]


class _ProxyBase:
    """A proxy class base for OTACacheDB that dispatches all requests into a threadpool."""

    DB_THREAD_POOL_SIZE = 1

    def _thread_initializer(self, db_f):
        """Init a db connection for each thread worker"""
        # NOTE: set init to False always as we only operate db when using proxy
        self._thread_local.db = OTACacheDB(db_f)

    def __init__(self, db_f: Union[str, Path]):
        """Init the database connecting thread pool."""
        self._thread_local = threading.local()
        # set thread_pool_size to 1 to make the db access
        # to make it totally concurrent.
        self._executor = ThreadPoolExecutor(
            max_workers=self.DB_THREAD_POOL_SIZE,
            initializer=self._thread_initializer,
            initargs=(db_f,),
        )

    def close(self):
        self._executor.shutdown(wait=True)


class AIO_OTACacheDBProxy(_ProxyBase):
    async def insert_entry(self, *cache_meta: CacheMeta) -> int:
        def _inner():
            _db: OTACacheDB = self._thread_local.db
            return _db.insert_entry(*cache_meta)

        return await asyncio.get_running_loop().run_in_executor(self._executor, _inner)

    async def lookup_entry(
        self, fd: ColumnDescriptor, _input: Any
    ) -> Optional[CacheMeta]:
        def _inner():
            _db: OTACacheDB = self._thread_local.db
            return _db.lookup_entry(fd, _input)

        return await asyncio.get_running_loop().run_in_executor(self._executor, _inner)

    async def remove_entries(self, fd: ColumnDescriptor, *_inputs: Any) -> int:
        def _inner():
            _db: OTACacheDB = self._thread_local.db
            return _db.remove_entries(fd, *_inputs)

        return await asyncio.get_running_loop().run_in_executor(self._executor, _inner)

    async def rotate_cache(self, bucket_idx: int, num: int) -> Optional[List[str]]:
        def _inner():
            _db: OTACacheDB = self._thread_local.db
            return _db.rotate_cache(bucket_idx, num)

        return await asyncio.get_running_loop().run_in_executor(self._executor, _inner)
