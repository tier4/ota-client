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
"""Control of the launch/shutdown of otaproxy according to sub ECUs' status."""


from __future__ import annotations

import asyncio
import logging
import multiprocessing as mp
import multiprocessing.context as mp_ctx
import shutil
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Optional

from ota_proxy import config as local_otaproxy_cfg
from ota_proxy import run_otaproxy
from otaclient.configs.cfg import cfg, proxy_info

logger = logging.getLogger(__name__)


class OTAProxyLauncher:
    """Launcher of start/stop otaproxy in subprocess."""

    def __init__(self, *, executor: ThreadPoolExecutor) -> None:
        self.enabled = proxy_info.enable_local_ota_proxy
        self.upper_otaproxy = (
            str(proxy_info.upper_ota_proxy) if proxy_info.upper_ota_proxy else ""
        )
        self._external_cache_mp = (
            cfg.EXTERNAL_CACHE_DEV_MOUNTPOINT
            if cfg.OTAPROXY_ENABLE_EXTERNAL_CACHE
            else None
        )

        self._lock = asyncio.Lock()
        # process start/shutdown will be dispatched to thread pool
        self._run_in_executor = partial(
            asyncio.get_event_loop().run_in_executor, executor
        )
        self._otaproxy_subprocess: mp_ctx.SpawnProcess | None = None

    @property
    def is_running(self) -> bool:
        return (
            self.enabled
            and self._otaproxy_subprocess is not None
            and self._otaproxy_subprocess.is_alive()
        )

    # API

    def cleanup_cache_dir(self) -> None:
        """
        NOTE: this method should only be called when all ECUs in the cluster
              are in SUCCESS ota_status(overall_ecu_status.all_success==True).
        """
        if (cache_dir := Path(local_otaproxy_cfg.BASE_DIR)).is_dir():
            logger.info("cleanup ota_cache on success")
            shutil.rmtree(cache_dir, ignore_errors=True)

    async def start(self, *, init_cache: bool) -> Optional[int]:
        """Start the otaproxy in a subprocess."""
        if not self.enabled or self._lock.locked() or self.is_running:
            return

        _spawn_ctx = mp.get_context("spawn")

        async with self._lock:
            otaproxy_subprocess = _spawn_ctx.Process(
                target=partial(
                    run_otaproxy,
                    host=str(proxy_info.local_ota_proxy_listen_addr),
                    port=proxy_info.local_ota_proxy_listen_port,
                    init_cache=init_cache,
                    cache_dir=local_otaproxy_cfg.BASE_DIR,
                    cache_db_f=local_otaproxy_cfg.DB_FILE,
                    upper_proxy=self.upper_otaproxy,
                    enable_cache=proxy_info.enable_local_ota_proxy_cache,
                    enable_https=proxy_info.gateway_otaproxy,
                    external_cache_mnt_point=self._external_cache_mp,
                ),
                daemon=True,
                name="otaproxy",
            )
            self._otaproxy_subprocess = otaproxy_subprocess
            await self._run_in_executor(otaproxy_subprocess.start)
            logger.info(
                f"otaproxy({otaproxy_subprocess.pid=}) started at "
                f"{proxy_info.local_ota_proxy_listen_addr}:{proxy_info.local_ota_proxy_listen_port}"
            )
            return otaproxy_subprocess.pid

    async def stop(self) -> None:
        """Stop the otaproxy subprocess.

        NOTE: This method only shutdown the otaproxy process, it will not cleanup the
              cache dir. cache dir cleanup is handled by other mechanism.
              Check cleanup_cache_dir API for more details.
        """
        if not self.enabled or self._lock.locked() or not self.is_running:
            return

        def _shutdown() -> None:
            if self._otaproxy_subprocess and self._otaproxy_subprocess.is_alive():
                logger.info("shuting down otaproxy server process...")
                self._otaproxy_subprocess.terminate()
                self._otaproxy_subprocess.join()
            self._otaproxy_subprocess = None

        async with self._lock:
            await self._run_in_executor(_shutdown)
            logger.info("otaproxy closed")
