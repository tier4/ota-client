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


import logging
import pytest
import pytest_mock
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from multiprocessing import Process
from pathlib import Path

from tests.utils import SlotMeta, run_http_server

logger = logging.getLogger(__name__)


@dataclass
class TestConfiguration:
    # module paths
    BOOT_CONTROL_COMMON_MODULE_PATH = "otaclient.app.boot_control._common"
    BOOT_CONTROL_CONFIG_MODULE_PATH = "otaclient.app.boot_control.configs"
    CONFIGS_MODULE_PATH = "otaclient.app.configs"
    CBOOT_MODULE_PATH = "otaclient.app.boot_control._cboot"
    GRUB_MODULE_PATH = "otaclient.app.boot_control._grub"
    RPI_BOOT_MODULE_PATH = "otaclient.app.boot_control._rpi_boot"
    OTACLIENT_MODULE_PATH = "otaclient.app.ota_client"
    OTACLIENT_STUB_MODULE_PATH = "otaclient.app.ota_client_stub"
    OTACLIENT_SERVICE_MODULE_PATH = "otaclient.app.ota_client_service"
    OTAMETA_MODULE_PATH = "otaclient.app.ota_metadata"
    OTAPROXY_MODULE_PATH = "otaclient.ota_proxy"
    CREATE_STANDBY_MODULE_PATH = "otaclient.app.create_standby"
    MAIN_MODULE_PATH = "otaclient.app.main"

    OTACLIENT_APP__CMDHELPER = "otaclient.app._cmdhelpers"

    # dummy ota-image setting
    OTA_IMAGE_DIR = "/ota-image"
    OTA_IMAGE_DATA_DIR = "/ota-image/data"
    METADATA_JWT_FNAME = "metadata.jwt"
    OTA_IMAGE_SERVER_ADDR = "127.0.0.1"
    OTA_IMAGE_SERVER_PORT = 8080
    OTA_IMAGE_URL = f"http://{OTA_IMAGE_SERVER_ADDR}:{OTA_IMAGE_SERVER_PORT}"
    KERNEL_VERSION = "5.8.0-53-generic"
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
    CMDLINE_SLOT_A = (
        f"BOOT_IMAGE=/vmlinuz-{KERNEL_VERSION} root=UUID={SLOT_A_UUID} ro quiet splash"
    )
    CMDLINE_SLOT_B = f"BOOT_IMAGE=/vmlinuz-{OTA_STANDBY_KERNEL_LABEL} root=UUID={SLOT_B_UUID} ro quiet splash"

    # otaproxy settings
    OTA_PROXY_SERVER_ADDR = "127.0.0.1"
    OTA_PROXY_SERVER_PORT = 18080


test_cfg = TestConfiguration()


@pytest.fixture(autouse=True, scope="session")
def run_http_server_subprocess():
    _server_p = Process(
        target=run_http_server,
        args=[test_cfg.OTA_IMAGE_SERVER_ADDR, test_cfg.OTA_IMAGE_SERVER_PORT],
        kwargs={"directory": test_cfg.OTA_IMAGE_DIR},
    )
    try:
        _server_p.start()
        # NOTE: wait for 2 seconds for the server to fully start
        time.sleep(2)
        logger.info(f"start background ota-image server on {test_cfg.OTA_IMAGE_URL}")
        yield
    finally:
        logger.info("shutdown background ota-image server")
        _server_p.kill()


@pytest.fixture(scope="class")
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
    #
    # ------ prepare slot_a ------ #
    #
    slot_a = tmp_path_factory.mktemp("slot_a")
    shutil.copytree(
        Path(test_cfg.OTA_IMAGE_DIR) / "data", slot_a, dirs_exist_ok=True, symlinks=True
    )
    # simulate the diff between local running image and target OTA image
    shutil.move(str(slot_a / "var"), slot_a / "var_old")
    shutil.move(str(slot_a / "usr"), slot_a / "usr_old")

    # manually create symlink to kernel and initrd.img
    vmlinuz_symlink = slot_a / "boot" / TestConfiguration.KERNEL_PREFIX
    vmlinuz_symlink.symlink_to(
        f"{TestConfiguration.KERNEL_PREFIX}-{TestConfiguration.KERNEL_VERSION}"
    )
    initrd_symlink = slot_a / "boot" / TestConfiguration.INITRD_PREFIX
    initrd_symlink.symlink_to(
        f"{TestConfiguration.INITRD_PREFIX}-{TestConfiguration.KERNEL_VERSION}"
    )

    #
    # ------ prepare slot_b ------ #
    #
    slot_b = tmp_path_factory.mktemp("slot_b")

    #
    # ------ prepare separated boot dev ------ #
    #
    slot_a_boot_dev = tmp_path_factory.mktemp("slot_a_boot")
    slot_b_boot_dev = tmp_path_factory.mktemp("slot_b_boot")

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


@pytest.fixture
def patch_cmdhelper(mocker: pytest_mock.MockerFixture):
    """Patch app.cmdhelper to prevent them actually calling cmd from host system."""
    _mocked_subprocess_call = mocker.MagicMock(return_value="")
    _mocked_subprocess_check_output = mocker.MagicMock(return_value="")

    mocker.patch(
        f"{test_cfg.OTACLIENT_APP__CMDHELPER}.subprocess_call", _mocked_subprocess_call
    )
    mocker.patch(
        f"{test_cfg.OTACLIENT_APP__CMDHELPER}.subprocess_check_output",
        _mocked_subprocess_check_output,
    )
