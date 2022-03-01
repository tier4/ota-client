import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

import logging
from typing import Any, List

from .config import config as cfg

logger = logging.getLogger(__name__)
logger.setLevel(cfg.LOG_LEVEL)


@dataclass
class CacheMeta:
    url: str = ""
    hash: str = ""
    size: int = 0
    content_type: str = ""
    content_encoding: str = ""

    def to_tuple(self) -> tuple:
        return (
            self.url,
            self.hash,
            self.size,
            self.content_type,
            self.content_encoding,
        )

    @classmethod
    def row_to_meta(cls, row: sqlite3.Row):
        res = cls()
        for k in OTACacheDB.COLUMNS:
            setattr(res, k, row[k])

        return res


class OTACacheDB:
    TABLE_NAME: str = "ota_cache"
    COLUMNS: dict = {
        "url": 0,
        "hash": 1,
        "size": 2,
        "content_type": 3,
        "content_encoding": 4,
    }
    INIT_DB: str = (
        f"CREATE TABLE {TABLE_NAME}("
        "url text UNIQUE PRIMARY KEY,"
        "hash text NOT NULL, "
        "size real NOT NULL,"
        "content_type text, "
        "content_encoding text)"
    )

    def __init__(self, db_file: str, init: bool = False):
        logger.debug("init database...")
        self._db_file = db_file
        self._wlock = Lock()
        self._closed = False

        self._connect_db(init)

    @contextmanager
    def _general_query(
        self,
        query: str,
        query_param: List[Any],
        multiple_query: bool = True,
        init: bool = False,
    ):
        if not init and self._closed:
            raise sqlite3.OperationalError("connect is closed")

        cur = self._con.cursor()
        if multiple_query:
            query_param = [(p,) for p in query_param]
            cur.executemany(query, query_param)
        else:
            cur.execute(query, query_param)

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
        with self._general_query(self.INIT_DB, (), multiple_query=False, init=True):
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
            multiple_query=False,
            init=True,
        ) as cur:
            if cur.fetchone() is None:
                self._init_table()

    def remove_url_by_hash(self, *hash: str):
        with self._wlock, self._general_query(
            f"DELETE FROM {self.TABLE_NAME} WHERE hash=?", hash
        ):
            self._con.commit()

    def remove_urls(self, *urls: str):
        with self._wlock, self._general_query(
            f"DELETE FROM {self.TABLE_NAME} WHERE url=?", urls
        ):
            self._con.commit()

    def insert_urls(self, *cache_meta: CacheMeta):
        rows = [m.to_tuple() for m in cache_meta]
        with self._wlock, self._general_query(
            f"INSERT OR REPLACE INTO {self.TABLE_NAME} VALUES (?,?,?,?,?)",
            rows,
            multiple_query=False,
        ):
            self._con.commit()

    def lookup_url(self, url: str) -> CacheMeta:
        with self._general_query(
            f"SELECT * FROM {self.TABLE_NAME} WHERE url=?", (url,), multiple_query=False
        ) as cur:
            return CacheMeta.row_to_meta(cur.fetchone())

    def lookup_all(self) -> CacheMeta:
        with self._general_query(
            f"SELECT * FROM {self.TABLE_NAME}", (), multiple_query=False
        ) as cur:
            for row in cur.fetchall():
                yield CacheMeta.row_to_meta(row)
