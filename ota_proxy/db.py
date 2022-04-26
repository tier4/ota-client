import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import make_dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock
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
    OTA_CACHE_IDX: List[str] = cfg.OTA_CACHE_IDX
    ROW_SHAPE = ",".join(["?"] * CacheMeta.shape())

    def __init__(self, db_file: str, init=False):
        logger.debug("init database...")
        self._db_file = db_file
        self._connect_db(init)

    def close(self):
        self._con.close()

    @contextmanager
    def _general_query(
        self, query: str, query_param: List[Any], /, *, is_multiple=True
    ):
        cur = self._con.cursor()
        _query_handler = cur.executemany if is_multiple else cur.execute

        # NOTE: no need to commit by ourselves as we are in autocommit mode now
        try:
            try:
                _query_handler(query, query_param)
                yield cur
            except sqlite3.Error as e:
                logger.debug(f"db {query=} error: {e!r}")
                raise e
        finally:
            # cleanup
            cur.close()

    @contextmanager
    def _transaction(self):
        # We must issue a "BEGIN" explicitly when running in auto-commit mode.
        self._con.execute("BEGIN")
        cur = self._con.cursor()
        try:
            yield cur
            self._con.commit()
        except sqlite3.Error:
            self._con.rollback()
            raise
        finally:
            cur.close()

    def _connect_db(self, init: bool):
        if init:
            Path(self._db_file).unlink(missing_ok=True)

        self._con = sqlite3.connect(
            self._db_file,
            check_same_thread=True,  # one thread per connection in the threadpool
            isolation_level=None,  # enable autocommit mode
        )
        self._con.row_factory = sqlite3.Row

        # check if the table exists/check whether the db file is valid
        with self._transaction() as cur:
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (self.TABLE_NAME,),
            )

            if cur.fetchone() is None:
                logger.warning(f"{self.TABLE_NAME} not found, init db...")
                # create ota_cache table
                cur.execute(self.INIT_OTA_CACHE, ())

                # create indices
                for idx in self.OTA_CACHE_IDX:
                    cur.execute(idx, ())

        # enable WAL mode
        self._con.execute("PRAGMA journal_mode=WAL;")

    def remove_entries_by_hashes(self, *hashes: str) -> int:
        _hashes = [(h,) for h in hashes]
        with self._general_query(
            f"DELETE FROM {self.TABLE_NAME} WHERE hash=?", _hashes
        ) as cur:
            return cur.rowcount

    def remove_entries_by_urls(self, *urls: str) -> int:
        _urls = [(u,) for u in urls]
        with self._general_query(
            f"DELETE FROM {self.TABLE_NAME} WHERE url=?", _urls
        ) as cur:
            return cur.rowcount

    def insert_entry(self, *cache_meta: CacheMeta) -> int:
        rows = [m.to_tuple() for m in cache_meta]
        with self._general_query(
            f"INSERT OR REPLACE INTO {self.TABLE_NAME} VALUES ({self.ROW_SHAPE})",
            rows,
        ) as cur:
            return cur.rowcount

    def lookup_url(self, url: str) -> CacheMeta:
        with self._general_query(
            f"SELECT * FROM {self.TABLE_NAME} WHERE url=?", (url,), is_multiple=False
        ) as cur:
            row = cur.fetchone()

        if row:
            # warm up the cache(update last_access timestamp) here
            res = CacheMeta.row_to_meta(row)
            with self._general_query(
                f"UPDATE {self.TABLE_NAME} SET last_access=? WHERE url=?",
                (datetime.now().timestamp(), res.url),
                is_multiple=False,
            ):
                pass

            return res
        else:
            return

    def lookup_all(self) -> List[CacheMeta]:
        with self._general_query(
            f"SELECT * FROM {self.TABLE_NAME}", (), is_multiple=False
        ) as cur:
            return [CacheMeta.row_to_meta(row) for row in cur.fetchall()]

    def rotate_cache(self, bucket: int, num: int) -> Union[str, None]:
        """Rotate cache entries in LRU flavour.

        Args:
            bucket: which bucket for space reserving
            num: num of entries needed to be deleted in this bucket

        Return:
            A list of hashes that needed to be deleted for space reserving,
                or None if no enough entries for space reserving.
        """
        # first, check whether we have required number of entries in the bucket
        with self._general_query(
            f"SELECT COUNT(*) FROM {self.TABLE_NAME} WHERE bucket=? ORDER BY last_access LIMIT ?",
            (bucket, num),
            is_multiple=False,
        ) as cur:
            _count = cur.fetchone()[0]

        # NOTE: if we can upgrade to sqlite3 >= 3.35,
        # use RETURNING clause instead of using 2 queries as below

        # if we have enough entries for space reserving
        if _count >= num:
            # first select those entries
            with self._general_query(
                (
                    f"SELECT * FROM {self.TABLE_NAME} "
                    "WHERE bucket=? "
                    "ORDER BY last_access "
                    "LIMIT ?"
                ),
                (bucket, num),
                is_multiple=False,
            ) as cur:
                _rows = cur.fetchall()

            # and then delete those entries with same conditions
            with self._general_query(
                (
                    f"DELETE FROM {self.TABLE_NAME} "
                    "WHERE bucket=? "
                    "ORDER BY last_access "
                    "LIMIT ?"
                ),
                (bucket, num),
                is_multiple=False,
            ):
                pass

            return [row["hash"] for row in _rows]


class DBProxy:
    """A proxy class for OTACacheDB that dispatches all requests into a threadpool."""

    def __init__(self, db_f: str, init=False):
        """Init the database connecting thread pool."""
        self._thread_local = threading.local()
        OTACacheDB(db_f, init=init)  # only for initializing db

        def _initializer():
            """Init a db connection for each thread worker"""
            self._thread_local.db = OTACacheDB(db_f, init=False)

        # 1 thread is enough according to small scale test
        self._db_executor = ThreadPoolExecutor(max_workers=1, initializer=_initializer)

    def close(self):
        self._db_executor.shutdown(wait=True)

    def __getattr__(self, key: str):
        """Passthrough method call by dot operator to threadpool."""

        def _outer(*args, **kwargs):
            def _inner():
                _db = self._thread_local.db
                _attr = getattr(_db, key)
                if not callable(_attr):
                    raise AttributeError

                return _attr(*args, **kwargs)

            # inner is dispatched to the db connection threadpool
            fut = self._db_executor.submit(_inner)
            return fut.result()

        return _outer
