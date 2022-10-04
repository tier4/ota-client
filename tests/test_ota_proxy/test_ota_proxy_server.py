import asyncio
import logging
import time
import pytest
import uvicorn
from hashlib import sha256
from urllib.parse import quote, unquote
from pytest_mock import MockerFixture
from pathlib import Path
from tests.conftest import cfg

logger = logging.getLogger(__name__)

# check the test_base/Dockerfile::SPECIAL_FILE
SPECIAL_FILE_NAME = r"path;adf.ae?qu.er\y=str#fragement"
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

    @pytest.fixture
    def test_inst(self):
        # NOTE:
        #   according to https://github.com/pytest-dev/pytest-asyncio/issues/297,
        #   the self in async fixture is no the test instance, so we use this fixture
        #   to get the test instance.
        return self

    @pytest.fixture(autouse=True)
    async def setup_ota_proxy_server(
        self, test_inst, tmp_path: Path, mocker: MockerFixture
    ):
        self = test_inst  # use real test inst as self, see test_inst fixture above
        import uvicorn
        from otaclient.ota_proxy import App, OTACache
        from otaclient.ota_proxy.config import Config

        ota_cache_dir = tmp_path / "ota-cache"
        ota_cache_dir.mkdir(parents=True, exist_ok=True)
        ota_cachedb = ota_cache_dir / "cachedb"
        self.ota_cache_dir = ota_cache_dir  # bind to test inst
        self.ota_cachedb = ota_cachedb
        # mock the ota-cache dir location
        ota_proxy_cfg = Config()
        ota_proxy_cfg.BASE_DIR = str(ota_cache_dir)
        ota_proxy_cfg.DB_FILE = str(ota_cachedb)
        mocker.patch(f"{cfg.OTAPROXY_MODULE_PATH}.ota_cache.cfg", ota_proxy_cfg)
        self.ota_proxy_cfg = ota_proxy_cfg

        # create a OTACache instance within the test process
        _ota_cache = OTACache(
            cache_enabled=True,
            upper_proxy="",
            enable_https=False,
            init_cache=True,
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
        try:
            await _start_uvicorn_server(otaproxy_inst)
            await asyncio.sleep(1)  # wait before otaproxy server is ready
            yield
        finally:
            try:
                await otaproxy_inst.shutdown()
            except Exception:
                pass  # ignore exp on shutting down

    async def test_download_file_with_special_fname(self):
        """
        Test the basic functionality of ota_proxy:
            download and cache a file with special name
        """
        import aiohttp
        import sqlite3

        # get the special file via otaproxy from the ota image server
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url=SPECIAL_FILE_URL, proxy=self.OTA_PROXY_URL
            ) as resp:
                assert resp.status == 200
                assert (resp_text := await resp.text())
        # assert the contents is the same across cache, response and original
        original = Path(SPECIAL_FILE_FPATH).read_text()
        cache_entry = Path(self.ota_cache_dir / SPECIAL_FILE_SHA256HASH).read_text()
        assert original == cache_entry == resp_text

        # shutdown the otaproxy server before inspecting the database
        await self.otaproxy_inst.shutdown()
        # assert the cache entry existed in the database
        with sqlite3.connect(self.ota_cachedb) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(f"SELECT * FROM {self.ota_proxy_cfg.TABLE_NAME}")
            # check db definition for details
            row = cur.fetchone()
            assert (
                row["url"] == unquote(SPECIAL_FILE_URL)
                and row["last_access"] < time.time()
                and row["hash"] == SPECIAL_FILE_SHA256HASH
                and row["size"] == len(SPECIAL_FILE_CONTENT.encode())
            )
