from __future__ import annotations

from hashlib import sha256
from os import PathLike
from typing import AsyncIterator

from anyio import open_file

from .config import config as cfg


async def read_file(fpath: PathLike) -> AsyncIterator[bytes]:
    """Open and read a file asynchronously."""
    async with await open_file(fpath, "rb") as f:
        while data := await f.read(cfg.CHUNK_SIZE):
            yield data


def url_based_hash(raw_url: str) -> str:
    """Generate sha256hash with unquoted raw_url."""
    _sha256_value = sha256(raw_url.encode()).hexdigest()
    return f"{cfg.URL_BASED_HASH_PREFIX}{_sha256_value}"
