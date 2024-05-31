from __future__ import annotations

import asyncio
from concurrent.futures import Executor
from hashlib import sha256
from os import PathLike
from typing import AsyncIterator

import aiofiles

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


async def read_file(fpath: PathLike, *, executor: Executor) -> AsyncIterator[bytes]:
    """Open and read a file asynchronously with aiofiles."""
    async with aiofiles.open(fpath, "rb", executor=executor) as f:
        while data := await f.read(cfg.CHUNK_SIZE):
            yield data


def url_based_hash(raw_url: str) -> str:
    """Generate sha256hash with unquoted raw_url."""
    _sha256_value = sha256(raw_url.encode()).hexdigest()
    return f"{cfg.URL_BASED_HASH_PREFIX}{_sha256_value}"
