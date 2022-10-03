import logging
import pytest
import uvicorn
from urllib.parse import quote
from pytest_mock import MockerFixture
from pathlib import Path
from tests.conftest import cfg

logger = logging.getLogger(__name__)


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
    # check the test_base/Dockerfile for details
    SPECIAL_FNAME = "path;adfae?query=str#fragement"
    SPECIAL_FILE_PATH = f"/data/{SPECIAL_FNAME}"
    SPECIAL_FILE_URL = f"{cfg.OTA_IMAGE_URL}{quote(SPECIAL_FILE_PATH)}"
    SPECIAL_FILE_FPATH = f"{cfg.OTA_IMAGE_DIR}/data/{SPECIAL_FNAME}"
    SPECIAL_FILE_SHA256HASH = (
        "68bfc21e4274ec0467cca829152eddff3994b6f29571ee7fe9ed7ec8839c3ece"
    )

    OTA_IMAGE_URL = f"http://{cfg.OTA_IMAGE_SERVER_ADDR}:{cfg.OTA_IMAGE_SERVER_PORT}"
    OTA_PROXY_URL = f"http://{cfg.OTA_PROXY_SERVER_ADDR}:{cfg.OTA_PROXY_SERVER_PORT}"

    @pytest.fixture
    async def setup_ota_proxy_server(self, tmp_path: Path, mocker: MockerFixture):
        import uvicorn
        from otaclient.ota_proxy import App, OTACache
        from otaclient.ota_proxy.config import Config

        ota_cache_dir = tmp_path / "ota-cache"
        ota_cache_dir.mkdir(parents=True, exist_ok=True)
        # mock the ota-cache dir location
        _ota_proxy_cfg = Config()
        _ota_proxy_cfg.BASE_DIR = str(ota_cache_dir)
        _ota_proxy_cfg.DB_FILE = str(ota_cache_dir / "cachedb")
        mocker.patch(f"{cfg.OTAPROXY_MODULE_PATH}.ota_cache.cfg", _ota_proxy_cfg)

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
        try:
            await _start_uvicorn_server(otaproxy_inst)
            yield ota_cache_dir, otaproxy_inst
        finally:
            try:
                await otaproxy_inst.shutdown()
            except Exception:
                pass  # ignore exp on shutting down

    @pytest.fixture(autouse=True)
    def manage_otaproxy_inst(self, setup_ota_proxy_server):
        """NOTE: bind the otaproxy inst to test instance here.

        according to https://github.com/pytest-dev/pytest-asyncio/issues/297,
        the self in async fixture is no the test instance, so we use a step method
        to bind the otaproxy_inst to the test instance.
        """
        self.ota_cache_dir, self.otaproxy_inst = setup_ota_proxy_server

    async def test_download_file_with_special_fname(self):
        """
        Test the basic functionality of ota_proxy:
            download and cache a file with special name
        """
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.get(
                url=self.SPECIAL_FILE_URL, proxy=self.OTA_PROXY_URL
            ) as resp:
                assert resp.status == 200
                assert (resp_text := await resp.text())
        # assert the contents is the same across cache, response and original
        original = Path(self.SPECIAL_FILE_FPATH).read_text()
        cache_entry = Path(
            self.ota_cache_dir / self.SPECIAL_FILE_SHA256HASH
        ).read_text()
        assert original == cache_entry == resp_text
