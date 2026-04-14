from __future__ import annotations

import os
import sys
from hashlib import sha256
from os import PathLike
from typing import AsyncGenerator
from urllib.parse import quote

import anyio
import yarl
from anyio import open_file

from otaclient_common._typing import StrOrPath

from .config import config as cfg

if sys.version_info < (3, 12):
    from itertools import islice

    def batched(iterable, n, *, strict=False):  # pragma: no cover
        """Python version of batched, copied from python documentation.

        See https://docs.python.org/3/library/itertools.html#itertools.batched.
        """
        # batched('ABCDEFG', 3) → ABC DEF G
        if n < 1:
            raise ValueError("n must be at least one")
        iterator = iter(iterable)
        while batch := tuple(islice(iterator, n)):
            if strict and len(batch) != n:
                raise ValueError("batched(): incomplete batch")
            yield batch

else:
    from itertools import batched  # noqa: F401


async def read_file(
    fpath: PathLike, chunk_size: int = cfg.LOCAL_READ_SIZE
) -> AsyncGenerator[bytes]:
    """Open and read a file asynchronously."""
    async with await open_file(fpath, "rb") as f:
        fd = f.wrapped.fileno()
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)
        while data := await f.read(chunk_size):
            yield data
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)


def read_file_once(fpath: StrOrPath | anyio.Path) -> bytes:
    with open(fpath, "rb") as f:
        fd = f.fileno()
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)
        data = f.read()
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    return data


def url_based_hash(raw_url: str) -> str:
    """Generate sha256hash with unquoted raw_url."""
    _sha256_value = sha256(raw_url.encode()).hexdigest()
    return f"{cfg.URL_BASED_HASH_PREFIX}{_sha256_value}"


def process_raw_url(raw_url: str, enable_https: bool) -> yarl.URL:
    """Process the raw URL received from upper uvicorn app.

    NOTE: raw_url(get from uvicorn) is unquoted, we must quote it again before we send it to the remote
    NOTE(20221003): as otaproxy, we should treat all contents after netloc as path and not touch it,
                    because we should forward the request as it to the remote.
    NOTE(20221003): unconditionally set scheme to https if enable_https, else unconditionally set to http
    NOTE(20260410): return yarl.URL to prevent aiohttp to encode the URL again.
    """
    # raw_url is "<scheme>://<netloc>/<path>..." — find the boundaries by string indexing
    _sep = raw_url.index("://") + 3
    try:
        _slash = raw_url.index("/", _sep)
    except ValueError:  # no path component
        _slash = len(raw_url)

    _netloc = raw_url[_sep:_slash]
    _scheme = "https" if enable_https else "http"
    _scheme_netloc: yarl.URL = yarl.URL(f"{_scheme}://{_netloc}")

    # everything after netloc, forwarded as-is with quoted back
    return _scheme_netloc.with_path(quote(raw_url[_slash:]), encoded=True)
