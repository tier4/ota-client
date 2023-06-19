from __future__ import annotations
import asyncio
import aiofiles
from concurrent.futures import Executor
from hashlib import sha256
from os import PathLike
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


async def read_file(fpath: PathLike, *, executor: Executor) -> AsyncIterator[bytes]:
    """Open and read a file asynchronously with aiofiles."""
    async with aiofiles.open(fpath, "rb", executor=executor) as f:
        while data := await f.read(cfg.CHUNK_SIZE):
            yield data
