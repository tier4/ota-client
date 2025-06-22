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


from __future__ import annotations

import logging

from .cache_control_header import OTAFileCacheControl
from .config import config
from .ota_cache import OTACache
from .server_app import App

logger = logging.getLogger(__name__)


__all__ = (
    "App",
    "OTACache",
    "OTAFileCacheControl",
    "config",
)


def run_otaproxy(
    host: str,
    port: int,
    *,
    init_cache: bool,
    cache_dir: str = config.BASE_DIR,
    cache_db_f: str = config.DB_FILE,
    upper_proxy: str,
    enable_cache: bool,
    enable_https: bool,
    external_cache_mnt_point: str | None = None,
):
    import asyncio

    import anyio
    import uvicorn
    import uvloop

    from . import App, OTACache

    _ota_cache = OTACache(
        base_dir=cache_dir,
        db_file=cache_db_f,
        cache_enabled=enable_cache,
        upper_proxy=upper_proxy,
        enable_https=enable_https,
        init_cache=init_cache,
        external_cache_mnt_point=external_cache_mnt_point,
    )
    _config = uvicorn.Config(
        App(_ota_cache),
        host=host,
        port=port,
        log_level="error",
        lifespan="on",
        loop="uvloop",
        # NOTE: must use h11, other http implementation will break HTTP proxy
        http="h11",
    )
    _server = uvicorn.Server(_config)

    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    anyio.run(
        _server.serve,
        backend="asyncio",
        backend_options={"loop_factory": uvloop.new_event_loop},
    )
