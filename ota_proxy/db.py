import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import make_dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Dict, List, Tuple, Union

from .config import config as cfg

import logging

logger = logging.getLogger(__name__)
logger.setLevel(cfg.LOG_LEVEL)


class _CacheMetaMixin:
    _cols = cfg.COLUMNS

    @classmethod
    def shape(cls) -> int:
        return len(cls._cols)

    def to_tuple(self) -> Tuple[Any]:
        return tuple([getattr(self, k) for k in self._cols])

    @classmethod
    def row_to_meta(cls, row: Dict[str, Any]):
        if not row:
            return

        res = cls()
        for k in cls._cols:
            setattr(res, k, row[k])

        return res


def make_cachemeta_cls(name: str):
    # set default value of each field as field type's zero value
    return make_dataclass(
        name,
        [(k, v.col_type, v.col_type()) for k, v in cfg.COLUMNS.items()],
        bases=(_CacheMetaMixin,),
    )


CacheMeta = make_cachemeta_cls("CacheMeta")
# fix the issue of pickling dynamically generated dataclass
CacheMeta.__module__ = __name__


class OTACacheDB:
    TABLE_NAME: str = cfg.TABLE_NAME
    INIT_OTA_CACHE: str = (
        f"CREATE TABLE {TABLE_NAME}("
        + ", ".join([f"{k} {v.col_def}" for k, v in cfg.COLUMNS.items()])
        + ")"
    )
    OTA_CACHE_IDX: List[str] = [
        cfg.BUCKET_LAST_ACCESS_IDX,
    ]
    ROW_SHAPE = ",".join(["?"] * CacheMeta.shape())

    def __init__(self, db_file: str, init=False):
        logger.debug("init database...")
        self._db_file = db_file
        self._connect_db(init)

    def close(self):
        self._con.close()

    def _connect_db(self, init: bool):
        if init:
            Path(self._db_file).unlink(missing_ok=True)

        self._con = sqlite3.connect(
            self._db_file,
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
                    con.execute(self.INIT_OTA_CACHE, ())

                    # create indices
                    for idx in self.OTA_CACHE_IDX:
                        con.execute(idx, ())

                ### db performance tunning
                # enable WAL mode
                con.execute("PRAGMA journal_mode = WAL;")
                # set synchronous mode
                con.execute("PRAGMA synchronous = normal;")
                # set temp_store to memory
                con.execute("PRAGMA temp_store = memory;")
                # enable mmap (size in bytes)
                mmap_size = 16 * 1024 * 1024  # 16MiB
                con.execute(f"PRAGMA mmap_size = {mmap_size};")
        except sqlite3.Error as e:
            logger.debug(f"init db failed: {e!r}")
            raise e

    def remove_entries_by_hashes(self, *hashes: str) -> int:
        _hashes = [(h,) for h in hashes]
        with self._con as con:
            cur = con.executemany(
                f"DELETE FROM {self.TABLE_NAME} WHERE hash=?", _hashes
            )

            return cur.rowcount

    def remove_entries_by_urls(self, *urls: str) -> int:
        _urls = [(u,) for u in urls]
        with self._con as con:
            cur = con.executemany(f"DELETE FROM {self.TABLE_NAME} WHERE url=?", _urls)
            return cur.rowcount

    def insert_entry(self, *cache_meta: CacheMeta) -> int:
        rows = [m.to_tuple() for m in cache_meta]
        with self._con as con:
            cur = con.executemany(
                f"INSERT OR REPLACE INTO {self.TABLE_NAME} VALUES ({self.ROW_SHAPE})",
                rows,
            )
            return cur.rowcount

    def lookup_url(self, url: str) -> CacheMeta:
        with self._con as con:
            cur = con.execute(
                f"SELECT * FROM {self.TABLE_NAME} WHERE url=?",
                (url,),
            )
            row = cur.fetchone()

            if row:
                # warm up the cache(update last_access timestamp) here
                res = CacheMeta.row_to_meta(row)
                cur = con.execute(
                    f"UPDATE {self.TABLE_NAME} SET last_access=? WHERE url=?",
                    (datetime.now().timestamp(), res.url),
                )

                return res
            else:
                return

    def lookup_all(self) -> List[CacheMeta]:
        with self._con as con:
            cur = con.execute(f"SELECT * FROM {self.TABLE_NAME}", ())
            return [CacheMeta.row_to_meta(row) for row in cur.fetchall()]

    def rotate_cache(self, bucket: int, num: int) -> Union[List[str], None]:
        """Rotate cache entries in LRU flavour.

        Args:
            bucket: which bucket for space reserving
            num: num of entries needed to be deleted in this bucket

        Return:
            A list of hashes that needed to be deleted for space reserving,
                or None if no enough entries for space reserving.
        """
        # first, check whether we have required number of entries in the bucket
        with self._con as con:
            cur = con.execute(
                f"SELECT COUNT(*) FROM {self.TABLE_NAME} WHERE bucket=? ORDER BY last_access LIMIT ?",
                (bucket, num),
            )
            _raw_res = cur.fetchone()
            if _raw_res is None:
                return

            # NOTE: if we can upgrade to sqlite3 >= 3.35,
            # use RETURNING clause instead of using 2 queries as below

            # if we have enough entries for space reserving
            if _raw_res[0] >= num:
                # first select those entries
                cur = con.execute(
                    (
                        f"SELECT * FROM {self.TABLE_NAME} "
                        "WHERE bucket=? "
                        "ORDER BY last_access "
                        "LIMIT ?"
                    ),
                    (bucket, num),
                )
                _rows = cur.fetchall()

                # and then delete those entries with same conditions
                con.execute(
                    (
                        f"DELETE FROM {self.TABLE_NAME} "
                        "WHERE bucket=? "
                        "ORDER BY last_access "
                        "LIMIT ?"
                    ),
                    (bucket, num),
                )

                return [row["hash"] for row in _rows]


def _proxy_wrapper(attr_n):
    def _wrapped(self, *args, **kwargs):
        # get the handler from underlaying db connector
        def _inner():
            _db = self._thread_local.db
            f = partial(getattr(_db, attr_n), *args, **kwargs)
            return f()

        # inner is dispatched to the db connection threadpool
        fut = self._executor.submit(_inner)
        return fut.result()

    return _wrapped


def _proxy_cls_factory(cls, *, target):
    for attr_n, attr in target.__dict__.items():
        if not attr_n.startswith("_") and callable(attr):
            # override method
            setattr(cls, attr_n, _proxy_wrapper(attr_n))

    return cls


@partial(_proxy_cls_factory, target=OTACacheDB)
class DBProxy:
    """A proxy class for OTACacheDB that dispatches all requests into a threadpool."""

    def __init__(self, db_f: str):
        """Init the database connecting thread pool."""
        self._thread_local = threading.local()

        def _initializer():
            """Init a db connection for each thread worker"""
            self._thread_local.db = OTACacheDB(db_f, init=False)

        self._executor = ThreadPoolExecutor(max_workers=6, initializer=_initializer)

    def close(self):
        self._executor.shutdown(wait=True)


del _proxy_cls_factory
