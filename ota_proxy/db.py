import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import make_dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Generator, List, Tuple

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
    TABLE_NAME = cfg.TABLE_NAME
    INIT_DB: str = (
        f"CREATE TABLE {cfg.TABLE_NAME}("
        + ", ".join([f"{k} {v.col_def}" for k, v in cfg.COLUMNS.items()])
        + ")"
    )
    ROW_SHAPE = ",".join(["?"] * CacheMeta.shape())

    def __init__(self, db_file: str, init=False):
        logger.debug("init database...")
        self._db_file = db_file
        self._connect_db(init)

    @contextmanager
    def _general_query(self, query: str, query_param: List[Any], /, *, init=False):
        cur = self._con.cursor()
        _query_handler = cur.execute

        try:
            _query_method = query.strip().split(" ")[0]
            if _query_method in {"INSERT", "DELETE"}:
                _query_handler = cur.executemany

            _query_handler(query, query_param)
            if _query_method in {"INSERT", "DELETE"}:
                self._con.commit()
            yield cur
        except sqlite3.Error as e:
            # if any exception happens, rollback the latest transaction
            self._con.rollback()
            logger.debug(f"db {query=} error: {e!r}")
        finally:
            cur.close()

    def _init_table(self):
        logger.debug("init sqlite database...")
        _cur = self._con.cursor()
        try:
            _cur.execute(self.INIT_DB, ())
            self._con.commit()
        except sqlite3.Error as e:
            logger.error(f"init table failed: {e!r}")
            raise
        finally:
            _cur.close()

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
        with self._general_query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (self.TABLE_NAME,),
            init=True,
        ) as cur:
            if cur.fetchone() is None:
                self._init_table()

            # enable WAL mode
            self._con.execute("PRAGMA journal_mode=WAL;")

    def remove_url_by_hash(self, *hash: str):
        hash = [(h,) for h in hash]
        with self._general_query(f"DELETE FROM {self.TABLE_NAME} WHERE hash=?", hash):
            pass

    def remove_urls(self, *urls: str):
        urls = [(u,) for u in urls]
        with self._general_query(f"DELETE FROM {self.TABLE_NAME} WHERE url=?", urls):
            pass

    def insert_urls(self, *cache_meta: CacheMeta):
        rows = [m.to_tuple() for m in cache_meta]
        with self._general_query(
            f"INSERT OR REPLACE INTO {self.TABLE_NAME} VALUES ({self.ROW_SHAPE})",
            rows,
        ):
            pass

    def lookup_url(self, url: str) -> CacheMeta:
        with self._general_query(
            f"SELECT * FROM {self.TABLE_NAME} WHERE url=?", (url,)
        ) as cur:
            return CacheMeta.row_to_meta(cur.fetchone())

    def lookup_all(self) -> Generator[CacheMeta, None, None]:
        with self._general_query(f"SELECT * FROM {self.TABLE_NAME}", ()) as cur:
            for row in cur.fetchall():
                yield CacheMeta.row_to_meta(row)


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
