# Copyright 2022 TIER IV, INC. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import asyncio
import logging
import multiprocessing
from abc import abstractmethod
from contextlib import AbstractContextManager
from functools import partial
from multiprocessing.context import SpawnProcess
from typing import Any, Callable, Coroutine, Dict, Optional, Protocol
from typing_extensions import ParamSpec, Self

from .cache_control import OTAFileCacheControl
from .server_app import App
from .ota_cache import OTACache
from .config import config

logger = logging.getLogger(__name__)


__all__ = (
    "App",
    "OTACache",
    "OTAFileCacheControl",
    "config",
    "OTAProxyContextProto",
    "subprocess_otaproxy_launcher",
)

_P = ParamSpec("_P")


async def run_otaproxy(
    host: str,
    port: int,
    *,
    init_cache: bool,
    cache_dir: str,
    cache_db_f: str,
    upper_proxy: str,
    enable_cache: bool,
    enable_https: bool,
    external_cache: Optional[str] = None,
):
    import uvicorn
    from . import App, OTACache

    _ota_cache = OTACache(
        base_dir=cache_dir,
        db_file=cache_db_f,
        cache_enabled=enable_cache,
        upper_proxy=upper_proxy,
        enable_https=enable_https,
        init_cache=init_cache,
        external_cache=external_cache,
    )
    _config = uvicorn.Config(
        App(_ota_cache),
        host=host,
        port=port,
        log_level="error",
        lifespan="on",
        loop="uvloop",
        http="h11",
    )
    _server = uvicorn.Server(_config)
    await _server.serve()


class OTAProxyContextProto(AbstractContextManager, Protocol):
    @abstractmethod
    def __init__(self, *args, **kwargs) -> None:
        ...

    @property
    def extra_kwargs(self) -> Dict[str, Any]:
        return {}

    @abstractmethod
    def __enter__(self) -> Self:
        ...


def _subprocess_main(
    subprocess_ctx: OTAProxyContextProto,
    otaproxy_entry: Callable[..., Coroutine],
):
    """Main entry for launching otaproxy server at subprocess."""
    import uvloop  # NOTE: only import uvloop at subprocess

    uvloop.install()
    with subprocess_ctx as ctx:
        asyncio.run(otaproxy_entry(**ctx.extra_kwargs))


def subprocess_otaproxy_launcher(
    subprocess_ctx: OTAProxyContextProto,
    otaproxy_entry: Callable[_P, Any] = run_otaproxy,
):
    """
    Returns:
        A callable main entry for launching otaproxy in subprocess.
    """

    def _inner(*args: _P.args, **kwargs: _P.kwargs) -> SpawnProcess:
        """Helper method to launch otaproxy in subprocess.

        This method works like a wrapper and passthrough all args and kwargs
        to the _subprocess_main function, and then execute the function in
        a subprocess.
        check _subprocess_main function for more details.
        """
        # prepare otaproxy coro
        _otaproxy_entry = partial(otaproxy_entry, *args, **kwargs)

        # run otaproxy in async loop in new subprocess
        mp_ctx = multiprocessing.get_context("spawn")
        otaproxy_subprocess = mp_ctx.Process(
            target=partial(_subprocess_main, subprocess_ctx, _otaproxy_entry),
            daemon=True,  # kill otaproxy if the parent process exists
        )
        otaproxy_subprocess.start()
        return otaproxy_subprocess

    return _inner
