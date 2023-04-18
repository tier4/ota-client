from __future__ import annotations
import asyncio
import aiofiles
from concurrent.futures import Executor
from hashlib import sha256
from os import PathLike, urandom
from typing import AsyncIterator

from .config import config as cfg


def get_backoff(n: int, factor: float, _max: float) -> float:
    return min(_max, factor * (2 ** (n - 1)))


async def wait_with_backoff(
    _retry_cnt: int, *, _backoff_factor: float, _backoff_max: float
) -> bool:
    """
    Returns:
        A bool indicates whether upper caller should keep retry.
    """
    _timeout = get_backoff(
        _retry_cnt,
        _backoff_factor,
        _backoff_max,
    )
    if _timeout <= _backoff_max:
        await asyncio.sleep(_timeout)
        return True
    return False


class AIOSHA256Hasher:
    def __init__(self, *, executor: Executor) -> None:
        self._executor = executor
        self._hashf = sha256()

    async def update(self, data: bytes):
        await asyncio.get_running_loop().run_in_executor(
            self._executor, self._hashf.update, data
        )

    async def hexdigest(self) -> str:
        return await asyncio.get_running_loop().run_in_executor(
            self._executor, self._hashf.hexdigest
        )


class TmpCacheFileNaming:
    """Naming scheme: tmp_<sha256_hash_of_URL>_<4_chars>"""

    PREFIX = cfg.TMP_FILE_PREFIX
    SEP = "_"

    @classmethod
    def get_url_hash(cls, url: str) -> str:
        return sha256(url.encode()).hexdigest()

    @classmethod
    def extract_url_hash(cls, tmp_fname: str) -> str:
        _tuple = tmp_fname.split(cls.SEP)
        if len(_tuple) != 3:
            raise ValueError(f"not a valid tmp_file_naming: {tmp_fname}")
        return _tuple[1]

    @classmethod
    def from_url(cls, url: str) -> str:
        return f"{cfg.TMP_FILE_PREFIX}{cls.SEP}{cls.get_url_hash(url)}{cls.SEP}{urandom(4).hex()}"


async def read_file(fpath: PathLike, *, executor: Executor) -> AsyncIterator[bytes]:
    """Open and read a file asynchronously with aiofiles."""
    async with aiofiles.open(fpath, "rb", executor=executor) as f:
        while data := await f.read(cfg.CHUNK_SIZE):
            yield data


async def verify_file(fpath: PathLike, _hash: str, *, executor: Executor) -> bool:
    _hash_f = sha256()
    async with aiofiles.open(fpath, "rb", executor=executor) as f:
        while data := await f.read(cfg.CHUNK_SIZE):
            _hash_f.update(data)
    return _hash_f.hexdigest() == _hash
