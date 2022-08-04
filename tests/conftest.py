import logging
import pytest
import shutil
from multiprocessing import Process
from pathlib import Path
from typing import Tuple

from tests.utils import run_http_server

logger = logging.getLogger(__name__)

# not enable proxy when doing test
DEFUALT_PROXY_INFO = """
enable_local_ota_proxy: false
"""

OTA_IMAGE_DIR = "/ota-image"
OTA_SERVER_ADDR = "127.0.0.1"
OTA_SERVER_PORT = 8080


@pytest.fixture(scope="session")
def proxy_cfg():
    import tempfile
    from app import proxy_info

    with tempfile.NamedTemporaryFile() as f:
        Path(f.name).write_text(DEFUALT_PROXY_INFO)
        return proxy_info.parse_proxy_info(proxy_info_file=f.name)


@pytest.fixture(autouse=True, scope="session")
def run_http_server_subprocess():
    _server_p = Process(
        target=run_http_server,
        args=[OTA_SERVER_ADDR, OTA_SERVER_PORT],
        kwargs={"directory": OTA_IMAGE_DIR},
    )
    logger.info(
        f"start background ota-image server at http://{OTA_SERVER_ADDR}:{OTA_SERVER_PORT}"
    )
    try:
        yield _server_p.start()
    finally:
        logger.info("shutdown background ota-image server")
        _server_p.kill()


@pytest.fixture(scope="session")
def ab_slots(tmp_path_factory: Path) -> Tuple[str, str]:
    """Prepare AB slots for the whole test session.

    The slot_a will be the active slot, it will be populated
    with the contents from /ota-image dir, with some of the dirs
    renamed to simulate version update.

    Structure:
        tmp_path_factory:
            slot_a/ (active, populated with ota-image)
            slot_b/ (standby)

    Return:
        A tuple includes the path to A/B slots respectly.
    """
    # prepare slot_a
    slot_a = tmp_path_factory / "slot_a"
    shutil.copytree(OTA_IMAGE_DIR, slot_a, dirs_exist_ok=True, symlinks=True)
    (slot_a / "var").rename("var_old")
    (slot_a / "usr").rename("lib_old")
    # prepare slot_b
    slot_b = tmp_path_factory / "slot_b"
    slot_b.mkdir()

    return str(slot_a), str(slot_b)
