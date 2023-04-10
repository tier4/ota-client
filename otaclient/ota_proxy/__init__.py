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
from functools import partial
from multiprocessing.context import SpawnProcess
from pathlib import Path
from typing import Callable

from .cache_control import OTAFileCacheControl
from .server_app import App
from .ota_cache import OTACache, OTACacheScrubHelper
from .config import config

logger = logging.getLogger(__name__)


__all__ = (
    "App",
    "OTACache",
    "OTACacheScrubHelper",
    "OTAFileCacheControl",
    "config",
    "subprocess_start_otaproxy",
)


def _subprocess_main(
    host: str,
    port: int,
    *,
    init_cache: bool,
    cache_dir: str,
    cache_db_f: str,
    upper_proxy: str,
    enable_cache: bool,
    enable_https: bool,
    subprocess_init: Callable,
):
    import uvloop

    # ------ pre-start callable ------ #
    subprocess_init()
    # ------ scrub cache folder if cache re-use is possible ------ #
    should_init_cache = init_cache or not (
        Path(cache_dir).is_dir() and Path(cache_db_f).is_file()
    )
    if not should_init_cache:
        scrub_helper = OTACacheScrubHelper(cache_db_f, cache_dir)
        try:
            scrub_helper.scrub_cache()
        except Exception as e:
            logger.error(f"scrub cache failed, force init: {e!r}")
            should_init_cache = True
        finally:
            del scrub_helper

    uvloop.install()
    asyncio.run(
        run_otaproxy(
            host=host,
            port=port,
            cache_dir=cache_dir,
            cache_db_f=cache_db_f,
            enable_cache=enable_cache,
            upper_proxy=upper_proxy,
            enable_https=enable_https,
            init_cache=init_cache,
        )
    )


def subprocess_start_otaproxy(*args, **kwargs) -> SpawnProcess:
    """Helper method to launch otaproxy in subprocess.

    This method works like a wrapper and passthrough all args and kwargs
    to the _subprocess_main function, and then execute the function in
    a subprocess.
    check _subprocess_main function for more details.
    """

    # run otaproxy in async loop in new subprocess
    mp_ctx = multiprocessing.get_context("spawn")
    otaproxy_subprocess = mp_ctx.Process(
        target=partial(_subprocess_main, *args, **kwargs),
        daemon=True,  # kill otaproxy if otaclient exists
    )
    otaproxy_subprocess.start()
    return otaproxy_subprocess


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
