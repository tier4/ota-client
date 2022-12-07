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


import aiohttp
import asyncio
import logging
import time
import pytest
import pytest_mock
import random
import shutil
import uvicorn
from hashlib import sha256
from urllib.parse import quote, unquote, urljoin
from pathlib import Path
from typing import List

from otaclient.app.ota_metadata import RegularInf
from tests.conftest import cfg

logger = logging.getLogger(__name__)

# check the test_base/Dockerfile::SPECIAL_FILE
SPECIAL_FILE_NAME = r"path;adf.ae?qu.er\y=str#fragファイルement"
SPECIAL_FILE_CONTENT = SPECIAL_FILE_NAME
SPECIAL_FILE_PATH = f"/data/{SPECIAL_FILE_NAME}"
SPECIAL_FILE_URL = f"{cfg.OTA_IMAGE_URL}{quote(SPECIAL_FILE_PATH)}"
SPECIAL_FILE_FPATH = f"{cfg.OTA_IMAGE_DIR}/data/{SPECIAL_FILE_NAME}"
SPECIAL_FILE_SHA256HASH = sha256(SPECIAL_FILE_CONTENT.encode()).hexdigest()


async def _start_uvicorn_server(server: uvicorn.Server):
    """NOTE: copied from Server.serve method, start method
    cannot be called directly.
    """
    config = server.config
    if not config.loaded:
        config.load()
    server.lifespan = config.lifespan_class(config)
    await server.startup()


class TestOTAProxyServer:
    OTA_IMAGE_URL = f"http://{cfg.OTA_IMAGE_SERVER_ADDR}:{cfg.OTA_IMAGE_SERVER_PORT}"
    OTA_PROXY_URL = f"http://{cfg.OTA_PROXY_SERVER_ADDR}:{cfg.OTA_PROXY_SERVER_PORT}"
    REGULARS_TXT_PATH = f"{cfg.OTA_IMAGE_DIR}/regulars.txt"
    CLIENTS_NUM = 6

    @pytest.fixture
    def test_inst(self):
        # NOTE:
        #   according to https://github.com/pytest-dev/pytest-asyncio/issues/297,
        #   the self in async fixture is no the test instance, so we use this fixture
        #   to get the test instance.
        return self

    @pytest.fixture
    async def setup_ota_proxy_server(self, test_inst, tmp_path: Path):
        self = test_inst  # use real test inst as self, see test_inst fixture above
        import uvicorn
        from otaclient.ota_proxy import App, OTACache

        ota_cache_dir = tmp_path / "ota-cache"
        ota_cache_dir.mkdir(parents=True, exist_ok=True)
        ota_cachedb = ota_cache_dir / "cachedb"
        self.ota_cache_dir = ota_cache_dir  # bind to test inst
        self.ota_cachedb = ota_cachedb

        # create a OTACache instance within the test process
        _ota_cache = OTACache(
            cache_enabled=True,
            upper_proxy="",
            enable_https=False,
            init_cache=True,
            base_dir=ota_cache_dir,
            db_file=ota_cachedb,
        )
        _config = uvicorn.Config(
            App(_ota_cache),
            host=cfg.OTA_PROXY_SERVER_ADDR,
            port=cfg.OTA_PROXY_SERVER_PORT,
            log_level="error",
            lifespan="on",
            loop="asyncio",
            http="h11",
        )
        otaproxy_inst = uvicorn.Server(_config)
        self.otaproxy_inst = otaproxy_inst
        self.ota_cache = _ota_cache

    @pytest.fixture(
        params=[
            "below_soft_limit",
            "below_hard_limit",
            "exceed_hard_limit",
        ],
        autouse=True,
    )
    async def launch_ota_proxy_server(
        self,
        mocker: pytest_mock.MockerFixture,
        test_inst,
        request,
        setup_ota_proxy_server,
    ):
        """
        NOTE: launch ota_proxy in different space availability condition
        """
        self = test_inst
        # patch the OTACache's space availability check method
        mocker.patch.object(
            self.ota_cache,
            "_background_check_free_space",
            mocker.MagicMock(),
        )
        # directly set the event
        self.space_availability = request.param
        if request.param == "below_soft_limit":
            self.ota_cache._storage_below_soft_limit_event.set()
            self.ota_cache._storage_below_hard_limit_event.set()
        elif request.param == "below_hard_limit":
            self.ota_cache._storage_below_soft_limit_event.clear()
            self.ota_cache._storage_below_hard_limit_event.set()
        elif request.param == "exceed_hard_limit":
            self.ota_cache._storage_below_soft_limit_event.clear()
            self.ota_cache._storage_below_hard_limit_event.clear()

        try:
            await _start_uvicorn_server(self.otaproxy_inst)
            await asyncio.sleep(1)  # wait before otaproxy server is ready
            yield
        finally:
            shutil.rmtree(self.ota_cache_dir, ignore_errors=True)
            try:
                await self.otaproxy_inst.shutdown()
            except Exception:
                pass  # ignore exp on shutting down

    @pytest.fixture(scope="class")
    def parse_regulars(self):
        regular_entries: List[RegularInf] = []
        with open(self.REGULARS_TXT_PATH, "r") as f:
            for _line in f:
                _entry = RegularInf.parse_reginf(_line)
                regular_entries.append(_entry)
        return regular_entries

    async def test_download_file_with_special_fname(self):
        """
        Test the basic functionality of ota_proxy under different space availability:
            download and cache a file with special name
        """
        import sqlite3
        from otaclient.ota_proxy import config as cfg

        # get the special file via otaproxy from the ota image server
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url=SPECIAL_FILE_URL, proxy=self.OTA_PROXY_URL
            ) as resp:
                assert resp.status == 200
                assert (resp_text := await resp.text(encoding="utf-8"))
        # assert the contents is the same across cache, response and original
        original = Path(SPECIAL_FILE_FPATH).read_text(encoding="utf-8")

        # shutdown the otaproxy server before inspecting the database
        await self.otaproxy_inst.shutdown()

        # Under different space availability, ota_proxy's behaviors are different
        # 1. below soft limit, cache is enabled and cache entry will be presented
        if self.space_availability == "below_soft_limit":
            cache_entry = Path(self.ota_cache_dir / SPECIAL_FILE_SHA256HASH).read_text(
                encoding="utf-8"
            )
            assert original == cache_entry == resp_text
            # assert the cache entry existed in the database
            with sqlite3.connect(self.ota_cachedb) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(f"SELECT * FROM {cfg.TABLE_NAME}")
                # check db definition for details
                row = cur.fetchone()
                assert (
                    row["url"] == unquote(SPECIAL_FILE_URL)
                    and row["last_access"] < time.time()
                    and row["sha256hash"] == SPECIAL_FILE_SHA256HASH
                    and row["size"] == len(SPECIAL_FILE_CONTENT.encode())
                )
        # 2. exceed soft limit, below hard limit
        #    cache is enabled, cache rotate will be executed, but since we only have one
        #    entry, cache rotate will fail and no cache entry will be left.
        elif self.space_availability == "below_hard_limit":
            pass
        # 3. exceed hard limit, cache will be disabled, no cache entry will be presented.
        elif self.space_availability == "exceed_hard_limit":
            pass

    async def ota_image_downloader(self, regular_entries, sync_event: asyncio.Event):
        """Test single client download the whole ota image."""
        async with aiohttp.ClientSession() as session:
            await sync_event.wait()
            await asyncio.sleep(random.randrange(100, 200) // 100)
            for entry in regular_entries:
                url = urljoin(
                    cfg.OTA_IMAGE_URL, quote(f'/data/{entry.path.relative_to("/")}')
                )

                # implement a simple retry here
                # NOTE: it might be some edge condition that subscriber subscribes on a just closed
                #       tracker, this will result in a hash mismatch, general a retry can solve this
                #       problem(the cached file is correct, only the caching stream is interrupted)
                count = 0
                while True:
                    async with session.get(url, proxy=self.OTA_PROXY_URL) as resp:
                        hash_f = sha256()
                        async for data, _ in resp.content.iter_chunks():
                            hash_f.update(data)

                        if hash_f.hexdigest() != entry.sha256hash:
                            count += 1
                            logger.error(
                                f"hash mismatch detected: {entry=}, {hash_f.hexdigest()=}, retry..."
                            )
                            if count > 6:
                                logger.error(
                                    f"retry {count} times failed: {entry=}, {hash_f.hexdigest()=}"
                                )
                                assert hash_f.hexdigest() == entry.sha256hash
                        else:
                            if count != 0:
                                logger.info(
                                    f"retry on {entry=} succeeded, {hash_f.hexdigest()=}"
                                )
                            break

    async def test_multiple_clients_download_ota_image(self, parse_regulars):
        """Test multiple client download the whole ota image simultaneously."""
        sync_event = asyncio.Event()
        tasks: List[asyncio.Task] = []
        for _ in range(self.CLIENTS_NUM):
            tasks.append(
                asyncio.create_task(
                    self.ota_image_downloader(parse_regulars, sync_event)
                )
            )
        logger.info(
            f"all {self.CLIENTS_NUM} clients have started to download ota image..."
        )
        sync_event.set()

        await asyncio.gather(*tasks, return_exceptions=False)
        await self.otaproxy_inst.shutdown()
        # check there is no tmp files left in the ota_cache dir
        # ensure that the gc for multi-cache-streaming works
        assert len(list(self.ota_cache_dir.glob("tmp_*"))) == 0
