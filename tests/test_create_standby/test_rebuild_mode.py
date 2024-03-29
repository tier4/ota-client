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


import shutil
import time
import typing
import pytest
from pathlib import Path
from pytest_mock import MockerFixture

from otaclient.app.boot_control import BootControllerProtocol
from otaclient.configs.app_cfg import Config as otaclient_Config

from tests.conftest import TestConfiguration as test_cfg
from tests.utils import SlotMeta, compare_dir

import logging

logger = logging.getLogger(__name__)


class Test_OTAupdate_with_create_standby_RebuildMode:
    """
    NOTE: the boot_control is mocked, only testing
          create_standby and the logics directly implemented by OTAUpdater.

    NOTE: testing the system using separated boot dev for each slots(like cboot).
    """

    @pytest.fixture
    def setup_test(self, tmp_path: Path, ab_slots: SlotMeta):
        #
        # ------ prepare ab slots ------ #
        #
        self.slot_a = Path(ab_slots.slot_a)
        self.slot_b = Path(ab_slots.slot_b)
        self.slot_a_boot_dir = Path(ab_slots.slot_a_boot_dev) / "boot"
        self.slot_b_boot_dir = Path(ab_slots.slot_b_boot_dev) / "boot"
        self.ota_image_dir = Path(test_cfg.OTA_IMAGE_DIR)

        self.otaclient_run_dir = tmp_path / "otaclient_run_dir"
        self.otaclient_run_dir.mkdir(parents=True, exist_ok=True)

        self.slot_a_boot_dir.mkdir(exist_ok=True, parents=True)
        self.slot_b_boot_dir.mkdir(exist_ok=True, parents=True)

        #
        # ------ prepare config ------ #
        #
        _otaclient_cfg = otaclient_Config(ACTIVE_ROOTFS=str(self.slot_a))
        self.otaclient_cfg = _otaclient_cfg

        # ------ prepare otaclient run dir ------ #
        Path(_otaclient_cfg.RUN_DPATH).mkdir(exist_ok=True, parents=True)

        #
        # ------ prepare mount space ------ #
        #
        Path(_otaclient_cfg.OTACLIENT_MOUNT_SPACE_DPATH).mkdir(
            exist_ok=True, parents=True
        )
        # directly point standby slot mp to self.slot_b
        _standby_slot_mp = Path(_otaclient_cfg.STANDBY_SLOT_MP)
        _standby_slot_mp.symlink_to(self.slot_b)

        # some important paths
        self.ota_metafiles_tmp_dir = Path(_otaclient_cfg.STANDBY_IMAGE_META_DPATH)
        self.ota_tmp_dir = Path(_otaclient_cfg.STANDBY_OTA_TMP_DPATH)

        yield
        # cleanup slot_b after test
        shutil.rmtree(self.slot_b, ignore_errors=True)

    @pytest.fixture(autouse=True)
    def mock_setup(self, mocker: MockerFixture, setup_test):
        # ------ mock boot_controller ------ #
        self._boot_control = typing.cast(
            BootControllerProtocol, mocker.MagicMock(spec=BootControllerProtocol)
        )
        self._boot_control.get_standby_boot_dir.return_value = self.slot_b_boot_dir

        # ------ mock otaclient cfg ------ #
        mocker.patch(f"{test_cfg.OTACLIENT_MODULE_PATH}.cfg", self.otaclient_cfg)
        mocker.patch(
            f"{test_cfg.CREATE_STANDBY_MODULE_PATH}.rebuild_mode.cfg",
            self.otaclient_cfg,
        )
        mocker.patch(f"{test_cfg.OTAMETA_MODULE_PATH}.cfg", self.otaclient_cfg)

    def test_update_with_create_standby_RebuildMode(self, mocker: MockerFixture):
        from otaclient.app.ota_client import _OTAUpdater, OTAClientControlFlags
        from otaclient.app.create_standby.rebuild_mode import RebuildMode

        # TODO: not test process_persistent currently,
        #       as we currently directly compare the standby slot
        #       with the OTA image.
        RebuildMode._process_persistents = mocker.MagicMock()

        # ------ execution ------ #
        otaclient_control_flags = typing.cast(
            OTAClientControlFlags, mocker.MagicMock(spec=OTAClientControlFlags)
        )
        _updater = _OTAUpdater(
            boot_controller=self._boot_control,
            create_standby_cls=RebuildMode,
            proxy=None,
            control_flags=otaclient_control_flags,
        )
        # NOTE: mock the shutdown method as we need to assert before the
        #       updater is closed.
        _updater_shutdown = _updater.shutdown
        _updater.shutdown = mocker.MagicMock()

        _updater.execute(
            version=test_cfg.UPDATE_VERSION,
            raw_url_base=test_cfg.OTA_IMAGE_URL,
            cookies_json=r'{"test": "my-cookie"}',
        )
        time.sleep(2)  # wait for downloader to record stats

        # ------ assertions ------ #
        # --- assert update finished
        _updater.shutdown.assert_called_once()
        otaclient_control_flags.wait_can_reboot_flag.assert_called_once()
        # --- ensure the update stats are collected
        _snapshot = _updater._update_stats_collector.get_snapshot()
        assert _snapshot.processed_files_num
        assert _snapshot.processed_files_size
        assert _snapshot.downloaded_files_num
        assert _snapshot.downloaded_files_size
        # assert _snapshot.downloaded_bytes
        # assert _snapshot.downloading_elapsed_time.export_pb().ToNanoseconds()
        assert _snapshot.update_applying_elapsed_time.export_pb().ToNanoseconds()

        # --- check slot creating result, ensure slot_a and slot_b is the same --- #
        # NOTE: merge contents from slot_b_boot_dir to slot_b
        shutil.copytree(self.slot_b_boot_dir, self.slot_b / "boot", dirs_exist_ok=True)
        # NOTE: for some reason tmp dir is created under OTA_IMAGE_DIR/data, but not listed
        # in the regulars.txt, so we create one here to make the test passed
        (self.slot_b / "tmp").mkdir(exist_ok=True)

        # NOTE: remove the ota-meta dir and ota-tmp dir to resolve the difference with OTA image
        shutil.rmtree(self.ota_metafiles_tmp_dir, ignore_errors=True)
        shutil.rmtree(self.ota_tmp_dir, ignore_errors=True)
        shutil.rmtree(self.slot_b / "opt/ota", ignore_errors=True)

        # --- check standby slot, ensure it is correctly populated
        compare_dir(Path(test_cfg.OTA_IMAGE_DIR) / "data", self.slot_b)

        # ------ finally close the updater ------ #
        _updater_shutdown()
