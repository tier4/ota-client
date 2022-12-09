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
import functools
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional, Type, Callable, Union, cast

from .config import config as cfg
from .orm import ColumnDescriptor, ORMBase

import logging

logger = logging.getLogger(__name__)


class CacheMeta(ORMBase):
    url: ColumnDescriptor[str] = ColumnDescriptor(
        str, "TEXT", "UNIQUE", "NOT NULL", "PRIMARY KEY", default="invalid_url"
    )
    bucket_idx: ColumnDescriptor[int] = ColumnDescriptor(
        int, "INTEGER", "NOT NULL", type_guard=True
    )
    last_access: ColumnDescriptor[int] = ColumnDescriptor(
        int, "INTEGER", "NOT NULL", type_guard=(int, float)
    )
    sha256hash: ColumnDescriptor[str] = ColumnDescriptor(
        str, "TEXT", "NOT NULL", default="invalid_hash"
    )
    size: ColumnDescriptor[int] = ColumnDescriptor(
        int, "INTEGER", "NOT NULL", type_guard=(int, float)
    )
    content_type: ColumnDescriptor[str] = ColumnDescriptor(str, "TEXT")
    content_encoding: ColumnDescriptor[str] = ColumnDescriptor(str, "TEXT")


class OTACacheDB:
    TABLE_NAME: str = cfg.TABLE_NAME
    OTA_CACHE_IDX: List[str] = [
        (
            "CREATE INDEX IF NOT EXISTS "
            f"bucket_last_access_idx_{TABLE_NAME} "
            f"ON {TABLE_NAME}({CacheMeta.bucket_idx.name}, {CacheMeta.last_access.name})"
        ),
    ]

    def __init__(self, db_file: Union[str, Path], init=False):
        """Connects to database(and initialize database if needed).

        If database doesn't have required table, or init==True,
        we will initialize the table here.

        Args:
            db_file: target to connect to
            init: whether to init database table or not

        Raise:
            Raises sqlite3.Error if database init/configuration failed.
        """
        if init:
            Path(db_file).unlink(missing_ok=True)

        self._con = sqlite3.connect(
            db_file,
            check_same_thread=True,  # one thread per connection in the threadpool
            # isolation_level=None,  # enable autocommit mode
        )
        self._con.row_factory = sqlite3.Row
        # check if the table exists/check whether the db file is valid
        try:
            with self._con as con:
                cur = con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (self.TABLE_NAME,),
                )
                if cur.fetchone() is None:
                    logger.warning(f"{self.TABLE_NAME} not found, init db...")
                    # create ota_cache table
                    con.execute(
                        CacheMeta.get_create_table_stmt(self.TABLE_NAME),
                        (),
                    )
                    # create indices
                    for idx in self.OTA_CACHE_IDX:
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

    def close(self):
        self._con.close()

    def remove_entries(self, fd: ColumnDescriptor, *_inputs: Any) -> int:
        """Remove entri(es) indicated by field(s).

        Args:
            fd: field descriptor of the column
            *_inputs: a list of field value(s)

        Returns:
            returns affected rows count.
        """
        if not _inputs:
            return 0
        if CacheMeta.contains_field(fd) and all(map(fd.check_type, _inputs)):
            with self._con as con:
                _regulated_input = [(i,) for i in _inputs]
                return con.executemany(
                    f"DELETE FROM {self.TABLE_NAME} WHERE {fd._field_name}=?",
                    _regulated_input,
                ).rowcount
        else:
            logger.debug(f"invalid inputs detected: {_inputs=}")
            return 0

    def lookup_entry(self, fd: ColumnDescriptor, _input: Any) -> Optional[CacheMeta]:
        """Lookup entry by field value.

        NOTE: lookup via this method will trigger update to <last_access> field

        Args:
            fd: field descriptor of the column
            _input: field value

        Returns:
            An instance of CacheMeta representing the cache entry, or None if lookup failed.
        """
        if not CacheMeta.contains_field(fd) or not fd.check_type(_input):
            return
        with self._con as con:  # put the lookup and update into one session
            if row := con.execute(
                f"SELECT * FROM {self.TABLE_NAME} WHERE {fd.name}=?",
                (_input,),
            ).fetchone():
                # warm up the cache(update last_access timestamp) here
                res = CacheMeta.row_to_meta(row)
                con.execute(
                    (
                        f"UPDATE {self.TABLE_NAME} SET {CacheMeta.last_access.name}=? "
                        f"WHERE {CacheMeta.url.name}=?"
                    ),
                    (int(datetime.now().timestamp()), res.url),
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
            A list of hashes that needed to be deleted for space reserving,
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
                return [row[CacheMeta.sha256hash.name] for row in _rows]


class _ProxyBase:
    """A proxy class base for OTACacheDB that dispatches all requests into a threadpool."""

    DB_THREAD_POOL_SIZE = 1

    def _thread_initializer(self, db_f):
        """Init a db connection for each thread worker"""
        # NOTE: set init to False always as we only operate db when using proxy
        self._thread_local.db = OTACacheDB(db_f, init=False)

    def __init__(self, db_f: Union[str, Path], *, init=False):
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


def _proxy_wrapper(func: Callable) -> Callable:
    """NOTE: this wrapper should only be used for _ProxyBase"""
    func_name = func.__name__

    @functools.wraps(func)
    def _wrapped(self, *args, **kwargs):
        # get the handler from underlaying db connector
        def _inner():
            _db: OTACacheDB = self._thread_local.db
            return getattr(_db, func_name)(*args, **kwargs)

        # inner is dispatched to the db connection threadpool
        return self._executor.submit(_inner).result()

    return _wrapped


def create_db_proxy(name: str, *, source_cls: Type) -> Type:
    """Class factory for creating a proxy class for OTACacheDB.

    All methods of OTACacheDB will be proxied to corresponding wrapper methods,
    the wrapper will proxy the request to the thread pool.
    """
    _new_cls = type(name, (_ProxyBase,), {})
    for attr_n, attr in source_cls.__dict__.items():
        # NOTE: not proxy the close method as we should call the proxy's close method
        if not attr_n.startswith("_") and callable(attr) and attr_n != "close":
            # assigned wrapped method to the proxy cls
            setattr(
                _new_cls,
                attr_n,
                _proxy_wrapper(attr),
            )
    return _new_cls


# expose OTACacheDB classs
OTACacheDBProxy = cast(
    Type[OTACacheDB], create_db_proxy("OTACacheDBProxy", source_cls=OTACacheDB)
)
del create_db_proxy  # cleanup namespace
