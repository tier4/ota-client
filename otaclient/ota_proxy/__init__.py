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


def subprocess_start_otaproxy(
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
) -> SpawnProcess:
    """Helper method to launch otaproxy in subprocess."""

    async def _async_main():
        import uvicorn

        ota_cache = OTACache(
            cache_enabled=enable_cache,
            upper_proxy=upper_proxy,
            enable_https=enable_https,
            init_cache=init_cache,
        )

        # NOTE: explicitly set loop and http options
        #       to prevent using wrong libs
        # NOTE 2: explicitly set http="h11",
        #       as http=="httptools" breaks ota_proxy functionality
        config = uvicorn.Config(
            App(ota_cache),
            host=host,
            port=port,
            log_level="error",
            lifespan="on",
            loop="asyncio",
            http="h11",
        )
        server = uvicorn.Server(config)
        await server.serve()

    def _subprocess_main():
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
        asyncio.run(_async_main())

    # run otaproxy in async loop in new subprocess
    mp_ctx = multiprocessing.get_context("spawn")
    otaproxy_subprocess = mp_ctx.Process(
        target=_subprocess_main,
        daemon=True,  # kill otaproxy if otaclient exists
    )
    otaproxy_subprocess.start()
    return otaproxy_subprocess
