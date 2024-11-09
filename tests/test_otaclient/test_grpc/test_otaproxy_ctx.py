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

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict

import pytest
from pytest_mock import MockerFixture

from ota_proxy import OTAProxyContextProto
from ota_proxy.config import Config as otaproxyConfig
from otaclient.grpc.api_v2.servicer import OTAProxyLauncher

logger = logging.getLogger(__name__)

OTAPROXY_CTX_MODULE = "otaclient.grpc._otaproxy_ctx"


class _DummyOTAProxyContext(OTAProxyContextProto):
    def __init__(self, sentinel) -> None:
        self.sentinel = sentinel

    @property
    def extra_kwargs(self) -> Dict[str, Any]:
        return {}

    def __enter__(self):
        logger.info(f"touch {self.sentinel=}")
        Path(self.sentinel).touch()
        return self

    def __exit__(self, __exc_type, __exc_value, __traceback):
        return


class TestOTAProxyLauncher:
    @pytest.fixture(autouse=True)
    async def mock_setup(
        self, mocker: MockerFixture, tmp_path: Path, proxy_info_fixture
    ):
        cache_base_dir = tmp_path / "ota_cache"
        self.sentinel_file = tmp_path / "otaproxy_sentinel"

        # ------ prepare mocked proxy_info and otaproxy_cfg ------ #
        self.proxy_info = proxy_info = proxy_info_fixture
        self.proxy_server_cfg = proxy_server_cfg = otaproxyConfig()
        proxy_server_cfg.BASE_DIR = str(cache_base_dir)  # type: ignore
        proxy_server_cfg.DB_FILE = str(cache_base_dir / "cache_db")  # type: ignore

        # ------ apply cfg patches ------ #
        mocker.patch(f"{OTAPROXY_CTX_MODULE}.proxy_info", proxy_info)
        mocker.patch(
            f"{OTAPROXY_CTX_MODULE}.local_otaproxy_cfg",
            proxy_server_cfg,
        )

        # init launcher inst
        threadpool = ThreadPoolExecutor()
        self.otaproxy_launcher = OTAProxyLauncher(
            executor=threadpool,
            subprocess_ctx=_DummyOTAProxyContext(str(self.sentinel_file)),
        )

        try:
            yield
        finally:
            threadpool.shutdown()

    async def test_start_stop(self):
        # startup
        # --- execution --- #
        _pid = await self.otaproxy_launcher.start(init_cache=True)
        await asyncio.sleep(3)  # wait for subprocess_init finish execution

        # --- assertion --- #
        assert self.otaproxy_launcher.is_running
        assert self.sentinel_file.is_file()
        assert _pid is not None and _pid > 0

        # shutdown
        # --- execution --- #
        # NOTE: save the subprocess ref as stop method will de-refer it
        _old_subprocess = self.otaproxy_launcher._otaproxy_subprocess
        await self.otaproxy_launcher.stop()

        # --- assertion --- #
        assert not self.otaproxy_launcher.is_running
        assert self.otaproxy_launcher._otaproxy_subprocess is None
        assert _old_subprocess and not _old_subprocess.is_alive()
