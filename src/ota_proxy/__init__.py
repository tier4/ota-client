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
    external_cache: str | None = None,
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
