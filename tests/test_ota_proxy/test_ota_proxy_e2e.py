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
import multiprocessing
import random
import shutil
import time
from functools import partial
from hashlib import sha256
from multiprocessing.context import SpawnProcess
from pathlib import Path
from urllib.parse import quote, unquote, urljoin

import aiohttp
import pytest
import uvicorn

import ota_proxy
from ota_metadata.legacy._parser import parse_regulars_from_txt
from ota_metadata.legacy._types import RegularInf
from ota_proxy.utils import url_based_hash
from tests.conftest import ThreadpoolExecutorFixtureMixin, cfg

logger = logging.getLogger(__name__)

MODULE = ota_proxy.__name__

# check the test_base/Dockerfile::SPECIAL_FILE
SPECIAL_FILE_NAME = r"path;adf.ae?qu.er\y=str#fragファイルement"
SPECIAL_FILE_CONTENT = SPECIAL_FILE_NAME
SPECIAL_FILE_PATH = f"/data/{SPECIAL_FILE_NAME}"
SPECIAL_FILE_URL = f"{cfg.OTA_IMAGE_URL}{quote(SPECIAL_FILE_PATH)}"
SPECIAL_FILE_FPATH = f"{cfg.OTA_IMAGE_DIR}/data/{SPECIAL_FILE_NAME}"
SPECIAL_FILE_SHA256HASH = sha256(SPECIAL_FILE_CONTENT.encode()).hexdigest()
REGULARS_TXT_PATH = f"{cfg.OTA_IMAGE_DIR}/regulars.txt"

CLIENTS_NUM = 3


def ota_proxy_process(condition: str, enable_cache_for_test: bool, ota_cache_dir: Path):
    import logging
    import os

    import anyio

    from ota_proxy import App, OTACache

    logging.basicConfig(level=logging.CRITICAL, force=True)
    logger = logging.getLogger("ota_proxy")
    logger.setLevel(logging.INFO)
    logger.info(f"otaproxy started: {os.getpid()=}")

    # NOTE:for below_hard_limit, first we let otaproxy runs under
    #      below_soft_limit to accumulate some entries,
    #      and then we switch to below_hard_limit
    # NOTE; for exceed_hard_limit, first we let otaproxy runs under
    #       below_soft_limit to accumulate some entires,
    #       and then switch to below_hard_limit to test LRU cache rotate,
    #       finally switch to exceed_hard_limit.
    def _mocked_background_check_freespace(self):
        flag_file = ota_cache_dir / "flag"
        flag_file.write_text(condition)

        _count = 0
        while not self._closed:
            if condition == "below_soft_limit":
                self._storage_below_soft_limit_event.set()
                self._storage_below_hard_limit_event.set()
            elif condition == "exceed_hard_limit":
                if _count < 5:
                    self._storage_below_soft_limit_event.set()
                    self._storage_below_hard_limit_event.set()
                elif _count < 10:
                    self._storage_below_soft_limit_event.clear()
                    self._storage_below_hard_limit_event.set()
                else:
                    self._storage_below_soft_limit_event.clear()
                    self._storage_below_hard_limit_event.clear()
            elif condition == "below_hard_limit":
                if _count < 10:
                    self._storage_below_soft_limit_event.set()
                    self._storage_below_hard_limit_event.set()
                else:
                    self._storage_below_soft_limit_event.clear()
                    self._storage_below_hard_limit_event.set()

            time.sleep(2)
            _count += 1

    OTACache._background_check_free_space = _mocked_background_check_freespace
    _ota_cache = OTACache(
        cache_enabled=enable_cache_for_test,
        upper_proxy="",
        enable_https=False,
        init_cache=True,
        base_dir=ota_cache_dir,
        db_file=ota_cache_dir / "cachedb",
    )
    _config = uvicorn.Config(
        App(_ota_cache),
        host=cfg.OTA_PROXY_SERVER_ADDR,
        port=cfg.OTA_PROXY_SERVER_PORT,
        log_level="error",
        lifespan="on",
        loop="uvloop",
        http="h11",
    )
    _server = uvicorn.Server(_config)
    anyio.run(_server.serve, backend="asyncio", backend_options={"use_uvloop": True})


@pytest.fixture(scope="module")
def parse_regulars():
    regular_entries: list[RegularInf] = []
    _count = 0
    with open(REGULARS_TXT_PATH, "r") as f:
        for _count, _line in enumerate(f, start=1):
            _entry = parse_regulars_from_txt(_line)
            regular_entries.append(_entry)
    logger.info(f"will test with {_count} entries to download ...")
    return regular_entries


# params: <storage_status>, <enable_cache>
OTA_PROXY_TEST_PARAMS = [
    ("below_soft_limit", True),
    ("below_hard_limit", True),
    ("exceed_hard_limit", True),
    ("below_soft_limit", False),
]


class TestOTAProxyServer(ThreadpoolExecutorFixtureMixin):
    THTREADPOOL_EXECUTOR_PATCH_PATH = f"{MODULE}.otacache"
    OTA_IMAGE_URL = f"http://{cfg.OTA_IMAGE_SERVER_ADDR}:{cfg.OTA_IMAGE_SERVER_PORT}"
    OTA_PROXY_URL = f"http://{cfg.OTA_PROXY_SERVER_ADDR}:{cfg.OTA_PROXY_SERVER_PORT}"

    @pytest.fixture(params=OTA_PROXY_TEST_PARAMS)
    def setup_ota_proxy_server(self, tmp_path: Path, request):
        logger.info(f"setup otaproxy with {request.param}")

        ota_cache_dir = tmp_path / "ota-cache"
        ota_cache_dir.mkdir(parents=True, exist_ok=True)
        ota_cachedb = ota_cache_dir / "cachedb"
        self.ota_cache_dir = ota_cache_dir  # bind to test inst
        self.ota_cachedb = ota_cachedb

        # patch the OTACache's space availability check method
        condition, enable_cache_for_test = request.param
        self.space_availability = condition
        self.enable_cache_for_test = enable_cache_for_test
        return partial(
            ota_proxy_process, condition, enable_cache_for_test, ota_cache_dir
        )

    @pytest.fixture(autouse=True)
    async def launch_ota_proxy_server(self, setup_ota_proxy_server):
        """
        NOTE: launch ota_proxy in different space availability condition
        """
        logger.info("launch otaproxy process ...")
        mp_ctx = multiprocessing.get_context("spawn")
        p = mp_ctx.Process(target=setup_ota_proxy_server)
        try:
            p.start()
            logger.info("wait 3 seconds for otaproxy process starts ...")
            await asyncio.sleep(3)  # wait before otaproxy server is ready

            # ensure the mocked background space checker is running, see
            #   ota_proxy_process above.
            if self.enable_cache_for_test:
                assert (self.ota_cache_dir / "flag").is_file()
            yield p
        finally:
            logger.info("shutting down otaproxy process ...")
            try:
                p.terminate()
                p.join()
            finally:
                shutil.rmtree(self.ota_cache_dir, ignore_errors=True)

    async def test_download_file_with_special_fname(
        self, launch_ota_proxy_server: SpawnProcess
    ):
        """
        Test the basic functionality of ota_proxy under different space availability:
            download and cache a file with special name
        """
        import sqlite3

        from ota_proxy import config as cfg

        # ------ get the special file via otaproxy from the ota image server ------ #
        # --- execution --- #
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url=SPECIAL_FILE_URL, proxy=self.OTA_PROXY_URL
            ) as resp:
                assert resp.status == 200
                assert (resp_text := await resp.text(encoding="utf-8"))

        # --- assertion --- #
        # 1. assert the contents is the same across cache, response and original
        original = Path(SPECIAL_FILE_FPATH).read_text(encoding="utf-8")

        # shutdown otaproxy server before checking
        launch_ota_proxy_server.terminate()
        launch_ota_proxy_server.join()

        # --- assertions --- #
        if not self.enable_cache_for_test:
            return

        special_file_url_based_sha256 = url_based_hash(unquote(SPECIAL_FILE_URL))
        # Under different space availability, ota_proxy's behaviors are different
        # 1. below soft limit, cache is enabled and cache entry will be presented
        if self.space_availability == "below_soft_limit":
            cache_entry = Path(
                self.ota_cache_dir / special_file_url_based_sha256
            ).read_text(encoding="utf-8")
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
                    and row["file_sha256"] == special_file_url_based_sha256
                    and row["cache_size"] == len(SPECIAL_FILE_CONTENT.encode())
                )
        # 2. exceed soft limit, below hard limit
        #    cache is enabled, cache rotate will be executed, but since we only have one
        #    entry, cache rotate will fail and no cache entry will be left.
        elif self.space_availability == "below_hard_limit":
            pass
        # 3. exceed hard limit, cache will be disabled, no cache entry will be presented.
        elif self.space_availability == "exceed_hard_limit":
            pass

    async def ota_image_downloader(
        self,
        worker_id: int,
        regular_entries: list[RegularInf],
        sync_event: asyncio.Event,
    ):
        """Test single client download the whole ota image."""
        async with aiohttp.ClientSession() as session:
            await sync_event.wait()
            await asyncio.sleep(random.randrange(100, 200) // 100)

            for count, entry in enumerate(regular_entries, start=1):
                if count % 1000 == 0:
                    logger.info(f"worker#{worker_id}: {count} finished ...")

                url = urljoin(
                    cfg.OTA_IMAGE_URL, quote(f"/data/{entry.relative_to('/')}")
                )

                _retry_count = 0
                _max_retry = 6
                # NOTE: for space_availability==exceed_hard_limit or below_hard_limit,
                #       it is normal that transition is interrupted when
                #       space_availability status transfered.
                while True:
                    async with session.get(
                        url,
                        proxy=self.OTA_PROXY_URL,
                        cookies={"acookie": "acookie", "bcookie": "bcookie"},
                    ) as resp:
                        hash_f = sha256()
                        read_size = 0
                        async for data, _ in resp.content.iter_chunks():
                            read_size += len(data)
                            hash_f.update(data)

                        try:
                            assert read_size == entry.size
                            assert hash_f.digest() == entry.sha256hash
                            break
                        except AssertionError:
                            _retry_count += 1
                            if _retry_count > _max_retry:
                                logger.error(f"failed on {entry}")
                                raise
                            logger.warning(
                                f"failed on {entry}, {_retry_count=}, still retry..."
                            )
            logger.info(f"worker#{worker_id} finished!")

    async def test_multiple_clients_download_ota_image(
        self, parse_regulars: list[RegularInf], launch_ota_proxy_server: SpawnProcess
    ):
        """Test multiple client download the whole ota image simultaneously."""
        # ------ dispatch many clients to download from otaproxy simultaneously ------ #
        # --- execution --- #
        sync_event = asyncio.Event()
        tasks: list[asyncio.Task] = []
        for worker_id in range(CLIENTS_NUM):
            tasks.append(
                asyncio.create_task(
                    self.ota_image_downloader(
                        worker_id,
                        parse_regulars,
                        sync_event,
                    )
                )
            )
        logger.info(f"all {CLIENTS_NUM} clients have started to download ota image...")
        sync_event.set()

        # --- assertions --- #
        # 1. ensure all clients finished the downloading successfully
        for _fut in asyncio.as_completed(tasks):
            await _fut

        # shutdown the otaproxy process before checking cache_dir
        launch_ota_proxy_server.terminate()
        launch_ota_proxy_server.join()

        # 2. check there is no tmp files left in the ota_cache dir
        #    ensure that the gc for multi-cache-streaming works
        assert not list(self.ota_cache_dir.glob("tmp_*"))
