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
OTA_IMAGE_SERVER_ADDR = "127.0.0.1"
OTA_IMAGE_SERVER_PORT = 8080


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
        args=[OTA_IMAGE_SERVER_ADDR, OTA_IMAGE_SERVER_PORT],
        kwargs={"directory": OTA_IMAGE_DIR},
    )
    logger.info(
        f"start background ota-image server at http://{OTA_IMAGE_SERVER_ADDR}:{OTA_IMAGE_SERVER_PORT}"
    )
    try:
        yield _server_p.start()
    finally:
        logger.info("shutdown background ota-image server")
        _server_p.kill()


@pytest.fixture(scope="session")
def ab_slots(tmp_path_factory: pytest.TempPathFactory) -> Tuple[str, str, str, str]:
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
    slot_a = tmp_path_factory.mktemp("slot_a")
    shutil.copytree(
        Path(OTA_IMAGE_DIR) / "data", slot_a, dirs_exist_ok=True, symlinks=True
    )
    # simulate the diff between versions
    shutil.move(str(slot_a / "var"), slot_a / "var_old")
    shutil.move(str(slot_a / "usr"), slot_a / "usr_old")
    # boot dir is a separated folder, so delete the boot folder under slot_a
    shutil.rmtree(slot_a / "boot", ignore_errors=True)
    # prepare slot_b
    slot_b = tmp_path_factory.mktemp("slot_b")

    # boot dir
    slot_a_boot_dir = tmp_path_factory.mktemp("slot_a_boot")
    shutil.copytree(
        Path(OTA_IMAGE_DIR) / "data/boot", slot_a_boot_dir, dirs_exist_ok=True
    )
    slot_b_boot_dir = tmp_path_factory.mktemp("slot_b_boot")
    return str(slot_a), str(slot_b), str(slot_a_boot_dir), str(slot_b_boot_dir)
