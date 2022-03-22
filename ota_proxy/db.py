import sqlite3
from dataclasses import make_dataclass
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

import logging
from typing import Any, Dict, List, Tuple

from .config import config as cfg

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

    def __init__(self, db_file: str, init=False):
        logger.debug("init database...")
        self._db_file = db_file
        self._wlock = Lock()
        self._closed = False

        self._connect_db(init)

    @contextmanager
    def _general_query(self, query: str, query_param: List[Any], /, *, init=False):
        if not init and self._closed:
            raise sqlite3.OperationalError("connect is closed")

        _query_method = query.strip().split(" ")[0]

        cur = self._con.cursor()
        _query_handler = cur.execute
        if _query_method in {"INSERT", "DELETE"}:
            _query_handler = cur.executemany

        _query_handler(query, query_param)

        try:
            yield cur
        finally:
            cur.close()

    def close(self):
        logger.debug("closing db...")
        if not self._closed:
            self._con.close()
            self._closed = True

    def _init_table(self):
        logger.debug("init sqlite database...")
        with self._general_query(self.INIT_DB, (), init=True):
            self._con.commit()

    def _connect_db(self, init: bool):
        if init:
            Path(self._db_file).unlink(missing_ok=True)

        self._con = sqlite3.connect(self._db_file, check_same_thread=False)
        self._con.row_factory = sqlite3.Row

        # check if the table exists
        with self._general_query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (self.TABLE_NAME,),
            init=True,
        ) as cur:
            if cur.fetchone() is None:
                self._init_table()

    def remove_url_by_hash(self, *hash: str):
        hash = [(h,) for h in hash]
        with self._wlock, self._general_query(
            f"DELETE FROM {self.TABLE_NAME} WHERE hash=?", hash
        ):
            self._con.commit()

    def remove_urls(self, *urls: str):
        urls = [(u,) for u in urls]
        with self._wlock, self._general_query(
            f"DELETE FROM {self.TABLE_NAME} WHERE url=?", urls
        ):
            self._con.commit()

    def insert_urls(self, *cache_meta: CacheMeta):
        rows = [m.to_tuple() for m in cache_meta]
        _row_shape = ",".join(["?"] * CacheMeta.shape())
        with self._wlock, self._general_query(
            f"INSERT OR REPLACE INTO {self.TABLE_NAME} VALUES ({_row_shape})",
            rows,
        ):
            self._con.commit()

    def lookup_url(self, url: str) -> CacheMeta:
        with self._general_query(
            f"SELECT * FROM {self.TABLE_NAME} WHERE url=?", (url,)
        ) as cur:
            return CacheMeta.row_to_meta(cur.fetchone())

    def lookup_all(self) -> CacheMeta:
        with self._general_query(f"SELECT * FROM {self.TABLE_NAME}", ()) as cur:
            for row in cur.fetchall():
                yield CacheMeta.row_to_meta(row)
