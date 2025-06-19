from __future__ import annotations

import os
from hashlib import sha256
from os import PathLike
from typing import AsyncGenerator
from urllib.parse import SplitResult, quote, urlsplit

from anyio import open_file

from .config import config as cfg


async def read_file(fpath: PathLike) -> AsyncGenerator[bytes]:
    """Open and read a file asynchronously."""
    async with await open_file(fpath, "rb") as f:
        fd = f.wrapped.fileno()
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)
        while data := await f.read(cfg.CHUNK_SIZE):
            yield data
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)


def url_based_hash(raw_url: str) -> str:
    """Generate sha256hash with unquoted raw_url."""
    _sha256_value = sha256(raw_url.encode()).hexdigest()
    return f"{cfg.URL_BASED_HASH_PREFIX}{_sha256_value}"


def process_raw_url(raw_url: str, enable_https: bool) -> str:
    """Process the raw URL received from upper uvicorn app.

    NOTE: raw_url(get from uvicorn) is unquoted, we must quote it again before we send it to the remote
    NOTE(20221003): as otaproxy, we should treat all contents after netloc as path and not touch it,
                    because we should forward the request as it to the remote.
    NOTE(20221003): unconditionally set scheme to https if enable_https, else unconditionally set to http
    """
    _raw_parse = urlsplit(raw_url)
    # get the base of the raw_url, which is <scheme>://<netloc>
    _raw_base = SplitResult(
        scheme=_raw_parse.scheme,
        netloc=_raw_parse.netloc,
        path="",
        query="",
        fragment="",
    ).geturl()

    # get the leftover part of URL besides base as path, and then quote it
    # finally, regenerate proper quoted url
    return SplitResult(
        scheme="https" if enable_https else "http",
        netloc=_raw_parse.netloc,
        path=quote(raw_url.replace(_raw_base, "", 1)),
        query="",
        fragment="",
    ).geturl()
