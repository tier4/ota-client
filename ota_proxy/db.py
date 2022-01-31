import sqlite3
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

import logging

logger = logging.getLogger(__name__)


@dataclass
class CacheMeta:
    url: str
    hash: str
    size: int
    content_type: str
    content_encoding: str

    def to_tuple(self) -> tuple:
        return (
            self.url,
            self.hash,
            self.size,
            self.content_type,
            self.content_encoding,
        )


class OTACacheDB:
    TABLE_NAME: str = "ota_cache"
    COLUMNS: dict = {
        "url": 0,
        "hash": 1,
        "size": 2,
        "content_type": 3,
        "content_encoding": 4,
    }

    def __init__(self, db_file: str, init: bool = False):
        logger.debug("init database...")
        self._db_file = db_file
        self._wlock = Lock()
        self._closed = False

        self._connect_db(init)

    def close(self):
        logger.debug("closing db...")
        if not self._closed:
            self._con.close()
            self._closed = True

    def _init_table(self):
        logger.debug("init sqlite database...")
        cur = self._con.cursor()

        # create the table
        cur.execute(
            f"""CREATE TABLE {self.TABLE_NAME}(
                    url text UNIQUE PRIMARY KEY, 
                    hash text NOT NULL, 
                    size real NOT NULL,
                    content_type text, 
                    content_encoding text)"""
        )

        self._con.commit()
        cur.close()

    def _connect_db(self, init: bool):
        if init:
            Path(self._db_file).unlink(missing_ok=True)
            self._con = sqlite3.connect(self._db_file, check_same_thread=False)
            self._init_table()
        else:
            self._con = sqlite3.connect(self._db_file, check_same_thread=False)

        # check if the table exists
        cur = self._con.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (self.TABLE_NAME,),
        )
        if cur.fetchone() is None:
            self._init_table()

    def remove_url_by_hash(self, *hash: str):
        if self._closed:
            raise sqlite3.OperationalError("connect is closed")

        with self._wlock:
            cur = self._con.cursor()
            cur.executemany(f"DELETE FROM {self.TABLE_NAME} WHERE hash=?", hash)

            self._con.commit()
            cur.close()
        return

    def remove_urls(self, *urls):
        if self._closed:
            raise sqlite3.OperationalError("connect is closed")

        with self._wlock:
            cur = self._con.cursor()
            cur.executemany(f"DELETE FROM {self.TABLE_NAME} WHERE url=?", urls)

            self._con.commit()
            cur.close()

    def insert_urls(self, *cache_meta: CacheMeta):
        if self._closed:
            raise sqlite3.OperationalError("connect is closed")

        rows = [m.to_tuple() for m in cache_meta]
        with self._wlock:
            cur = self._con.cursor()
            cur.executemany(
                f"INSERT OR REPLACE INTO {self.TABLE_NAME} VALUES (?,?,?,?,?)", rows
            )

            self._con.commit()
            cur.close()

    def lookup_url(self, url: str) -> CacheMeta:
        if self._closed:
            raise sqlite3.OperationalError("connect is closed")

        cur = self._con.cursor()
        cur.execute(f"SELECT * FROM {self.TABLE_NAME} WHERE url=?", (url,))
        row = cur.fetchone()
        if not row:
            return

        res = CacheMeta(
            url=row[self.COLUMNS["url"]],
            hash=row[self.COLUMNS["hash"]],
            size=row[self.COLUMNS["size"]],
            content_type=row[self.COLUMNS["content_type"]],
            content_encoding=row[self.COLUMNS["content_encoding"]],
        )
        return res
