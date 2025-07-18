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
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from multiprocessing import Process
from pathlib import Path
from queue import Queue
from typing import Any, Generator

import pytest
import pytest_mock

from otaclient._status_monitor import (
    TERMINATE_SENTINEL,
    OTAClientStatusCollector,
    StatusReport,
)
from otaclient.configs import ECUInfo, ProxyInfo
from otaclient.configs._ecu_info import parse_ecu_info
from otaclient.configs._proxy_info import parse_proxy_info
from tests.utils import SlotMeta, run_http_server

logger = logging.getLogger(__name__)

TEST_DIR = Path(__file__).parent

# see test base Dockerfile for more details.
OTA_IMAGE_DIR = Path("/ota-image")
CERTS_DIR = Path("/certs")
KERNEL_PREFIX = "vmlinuz"
INITRD_PREFIX = "initrd.img"

# local OTA image HTTP server
OTA_IMAGE_SERVER_ADDR = "127.0.0.1"
OTA_IMAGE_SERVER_PORT = 8080
OTA_IMAGE_URL = f"http://{OTA_IMAGE_SERVER_ADDR}:{OTA_IMAGE_SERVER_PORT}"
OTA_IMAGE_SIGN_CERT = OTA_IMAGE_DIR / "sign.pem"


def _get_kernel_version() -> str:
    boot_dir = OTA_IMAGE_DIR / "data/boot"
    _kernel_pa = f"{KERNEL_PREFIX}-*"
    _kernel = list(boot_dir.glob(_kernel_pa))[0]
    return _kernel.name.split("-", maxsplit=1)[1]


KERNEL_VERSION = _get_kernel_version()


@dataclass
class TestConfiguration:
    # module paths
    BOOT_CONTROL_COMMON_MODULE_PATH = "otaclient.boot_control._common"
    BOOT_CONTROL_CONFIG_MODULE_PATH = "otaclient.boot_control.configs"
    CONFIGS_MODULE_PATH = "otaclient.app.configs"
    GRUB_MODULE_PATH = "otaclient.boot_control._grub"
    RPI_BOOT_MODULE_PATH = "otaclient.boot_control._rpi_boot"
    OTACLIENT_MODULE_PATH = "otaclient.app.ota_client"
    OTACLIENT_STUB_MODULE_PATH = "otaclient.app.ota_client_stub"
    OTAMETA_MODULE_PATH = "ota_metadata.legacy.parser"

    # dummy ota-image setting
    OTA_IMAGE_DIR = "/ota-image"
    CERTS_DIR = "/certs"
    OTA_IMAGE_SERVER_ADDR = "127.0.0.1"
    OTA_IMAGE_SERVER_PORT = 8080
    OTA_IMAGE_URL = f"http://{OTA_IMAGE_SERVER_ADDR}:{OTA_IMAGE_SERVER_PORT}"
    KERNEL_VERSION = str(KERNEL_VERSION)
    CURRENT_VERSION = "123.x"
    UPDATE_VERSION = "789.x"

    # slots settings for testing
    # NOTE: grub use UUID and cboot use PARTUUID, SLOT_<slot>_UUID/PARTUUID are different
    #       things, just happens to have the same value for only for test convenience,
    SLOT_A_UUID = "aaaaaaaa-0000-0000-0000-aaaaaaaaaaaa"
    SLOT_A_PARTUUID = SLOT_A_UUID
    SLOT_B_UUID = "bbbbbbbb-1111-1111-1111-bbbbbbbbbbbb"
    SLOT_B_PARTUUID = SLOT_B_UUID
    SLOT_A_ID_GRUB = "sda2"
    SLOT_B_ID_GRUB = "sda3"
    SLOT_A_ID_CBOOT = "0"
    SLOT_B_ID_CBOOT = "1"

    # common configuration
    OTA_DIR = "/boot/ota"
    BOOT_DIR = "/boot"
    OTA_KERNEL_LABEL = "ota"
    OTA_STANDBY_KERNEL_LABEL = "ota.standby"

    # cboot specific conf
    OTA_STATUS_DIR = "/boot/ota-status"
    OTA_PARTITION_DIRNAME = "ota-partition"

    # grub specific conf
    KERNEL_PREFIX = "vmlinuz"
    INITRD_PREFIX = "initrd.img"
    GRUB_FILE = "/boot/grub/grub.cfg"
    DEFAULT_GRUB_FILE = "/etc/default/grub"
    FSTAB_FILE = "/etc/fstab"

    # otaproxy settings
    OTA_PROXY_SERVER_ADDR = "127.0.0.1"
    OTA_PROXY_SERVER_PORT = 18080


cfg = TestConfiguration()


@pytest.fixture(autouse=True, scope="session")
def run_http_server_subprocess():
    _server_p = Process(
        target=run_http_server,
        args=[cfg.OTA_IMAGE_SERVER_ADDR, cfg.OTA_IMAGE_SERVER_PORT],
        kwargs={"directory": cfg.OTA_IMAGE_DIR},
        daemon=True,
    )
    try:
        _server_p.start()
        # NOTE: wait for 2 seconds for the server to fully start
        time.sleep(2)
        logger.info(f"start background ota-image server on {cfg.OTA_IMAGE_URL}")
        yield
    finally:
        logger.info("shutdown background ota-image server")
        _server_p.kill()


@pytest.fixture
def ab_slots(tmp_path_factory: pytest.TempPathFactory) -> SlotMeta:
    """Prepare AB slots for the whole test session.

    The slot_a will be the active slot, it will be populated
    with the contents from /ota-image dir, with some of the dirs
    renamed to simulate version update.

    Structure:
        tmp_path_factory:
            slot_a/ (partuuid(cboot)/uuid(grub)=aaaaaaaa-0000-0000-0000-aaaaaaaaaaaa) (active, populated with ota-image)
            slot_b/ (partuuid(cboot)/uuid(grub)=bbbbbbbb-1111-1111-1111-bbbbbbbbbbbb) (standby)

    Return:
        A tuple includes the path to A/B slots respectly.
    """
    logger.info("creating ab_slots for testing ...")
    # prepare slot_a
    slot_a = tmp_path_factory.mktemp("slot_a")
    shutil.copytree(
        Path(cfg.OTA_IMAGE_DIR) / "data", slot_a, dirs_exist_ok=True, symlinks=True
    )
    # simulate the diff between versions
    shutil.move(str(slot_a / "var"), slot_a / "var_old")

    # manually create symlink to kernel and initrd.img
    vmlinuz_symlink = slot_a / "boot" / TestConfiguration.KERNEL_PREFIX
    initrd_symlink = slot_a / "boot" / TestConfiguration.INITRD_PREFIX

    try:
        vmlinuz_symlink.symlink_to(
            f"{TestConfiguration.KERNEL_PREFIX}-{TestConfiguration.KERNEL_VERSION}"
        )
        initrd_symlink.symlink_to(
            f"{TestConfiguration.INITRD_PREFIX}-{TestConfiguration.KERNEL_VERSION}"
        )
    except FileExistsError:
        pass

    # prepare slot_b
    slot_b = tmp_path_factory.mktemp("slot_b")

    # boot dev
    slot_a_boot_dev = tmp_path_factory.mktemp("slot_a_boot")
    slot_a_boot_dir = slot_a_boot_dev / "boot"
    slot_a_boot_dir.mkdir()
    shutil.copytree(
        Path(cfg.OTA_IMAGE_DIR) / "data/boot", slot_a_boot_dir, dirs_exist_ok=True
    )
    slot_b_boot_dev = tmp_path_factory.mktemp("slot_b_boot")
    slot_b_boot_dir = slot_b_boot_dev / "boot"
    slot_b_boot_dir.mkdir()
    (slot_b_boot_dir / "grub").mkdir()

    return SlotMeta(
        slot_a=str(slot_a),
        slot_b=str(slot_b),
        slot_a_boot_dev=str(slot_a_boot_dev),
        slot_b_boot_dev=str(slot_b_boot_dev),
    )


class ThreadpoolExecutorFixtureMixin:
    THTREADPOOL_EXECUTOR_PATCH_PATH: str

    @pytest.fixture
    def setup_executor(self, mocker: pytest_mock.MockerFixture):
        try:
            self._executor = ThreadPoolExecutor()
            mocker.patch(
                f"{self.THTREADPOOL_EXECUTOR_PATCH_PATH}.ThreadPoolExecutor",
                return_value=self._executor,
            )
            logger.info(
                f"ThreadpoolExecutor is patched at {self.THTREADPOOL_EXECUTOR_PATCH_PATH}"
            )
            yield
        finally:
            self._executor.shutdown()


ECU_INFO_YAML = """\
format_vesrion: 1
ecu_id: "autoware"
ip_addr: "10.0.0.1"
bootloader: "grub"
secondaries:
    - ecu_id: "p1"
      ip_addr: "10.0.0.11"
    - ecu_id: "p2"
      ip_addr: "10.0.0.12"
available_ecu_ids:
    - "autoware"
    # p1: new otaclient
    - "p1"
    # p2: old otaclient
    - "p2"
"""

PROXY_INFO_YAML = """\
gateway_otaproxy: false,
enable_local_ota_proxy: true
local_ota_proxy_listen_addr: "127.0.0.1"
local_ota_proxy_listen_port: 8082
"""


@pytest.fixture
def ecu_info_fixture(tmp_path: Path) -> ECUInfo:
    _yaml_f = tmp_path / "ecu_info.yaml"
    _yaml_f.write_text(ECU_INFO_YAML)
    _, res = parse_ecu_info(_yaml_f)
    return res


@pytest.fixture
def proxy_info_fixture(tmp_path: Path) -> ProxyInfo:
    _yaml_f = tmp_path / "proxy_info.yaml"
    _yaml_f.write_text(PROXY_INFO_YAML)
    _, res = parse_proxy_info(_yaml_f)
    return res


MAX_TRACEBACK_SIZE = 2048


@pytest.fixture(scope="class")
def ota_status_collector(
    class_mocker: pytest_mock.MockerFixture,
) -> Generator[tuple[OTAClientStatusCollector, Queue[StatusReport]], Any, None]:
    _shm_mock = class_mocker.MagicMock()

    _report_queue: Queue[StatusReport] = Queue()
    _status_collector = OTAClientStatusCollector(
        msg_queue=_report_queue,
        shm_status=_shm_mock,
        max_traceback_size=MAX_TRACEBACK_SIZE,
    )
    _collector_thread = _status_collector.start()

    try:
        yield _status_collector, _report_queue
    finally:
        _report_queue.put_nowait(TERMINATE_SENTINEL)
        _collector_thread.join()


@pytest.fixture(autouse=True)
def mock_ensure_mount(mocker: pytest_mock.MockerFixture) -> None:
    mocker.patch("otaclient.ota_core.ensure_mount")


@pytest.fixture(autouse=True)
def mock_ensure_umount(mocker: pytest_mock.MockerFixture) -> None:
    mocker.patch("otaclient.ota_core.ensure_umount")
