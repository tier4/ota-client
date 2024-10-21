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
import queue
import shutil
import time
import typing
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from otaclient import create_standby
from otaclient.app.configs import BaseConfig
from otaclient.app.configs import config as otaclient_cfg
from otaclient.boot_control import BootControllerProtocol
from otaclient.create_standby.rebuild_mode import RebuildMode
from otaclient.otaclient import OTAClientControlFlags, _OTAUpdater
from otaclient.stats_monitor import TERMINATE_SENTINEL, OTAClientStatsCollector
from tests.conftest import TestConfiguration as cfg
from tests.utils import SlotMeta, compare_dir

logger = logging.getLogger(__name__)

MODULE = create_standby.__name__


class TestOTAUpdateWithStandbySlotRebuildMode:
    """
    Configurations:
        1. creat_standby: rebuild mode

    NOTE: the boot_control is mocked, only testing
          create_standby and the logics directly implemented by OTAUpdater
    """

    SESSION_ID = "session_id"

    @pytest.fixture(scope="class")
    def msg_queue(self):
        return queue.Queue()

    @pytest.fixture(scope="class", autouse=True)
    def stats_monitor(self, msg_queue: queue.Queue):
        t = None
        try:
            _stats_monitor_instance = OTAClientStatsCollector(msg_queue)
            t = _stats_monitor_instance.start()
            yield _stats_monitor_instance
        finally:
            msg_queue.put_nowait(TERMINATE_SENTINEL)
            if t:
                t.join()

    @pytest.fixture
    def prepare_ab_slots(self, tmp_path: Path, ab_slots: SlotMeta):
        self.slot_a = Path(ab_slots.slot_a)
        self.slot_b = Path(ab_slots.slot_b)
        self.slot_a_boot_dir = Path(ab_slots.slot_a_boot_dev) / "boot"
        self.slot_b_boot_dir = Path(ab_slots.slot_b_boot_dev) / "boot"
        self.ota_image_dir = Path(cfg.OTA_IMAGE_DIR)

        self.otaclient_run_dir = tmp_path / "otaclient_run_dir"
        self.otaclient_run_dir.mkdir(parents=True, exist_ok=True)
        # ------ cleanup and prepare slot_b ------ #
        shutil.rmtree(self.slot_b, ignore_errors=True)
        self.slot_b.mkdir(exist_ok=True)
        # some important paths
        self.ota_metafiles_tmp_dir = self.slot_b / Path(
            otaclient_cfg.OTA_TMP_META_STORE
        ).relative_to("/")
        self.ota_tmp_dir = self.slot_b / Path(otaclient_cfg.OTA_TMP_STORE).relative_to(
            "/"
        )

        yield
        # cleanup slot_b after test
        shutil.rmtree(self.slot_b, ignore_errors=True)

    @pytest.fixture(autouse=True)
    def mock_setup(self, mocker: MockerFixture, prepare_ab_slots):

        # ------ mock boot_controller ------ #
        self._boot_control = typing.cast(
            BootControllerProtocol,
            mocker.MagicMock(
                spec=BootControllerProtocol, return_value=self.slot_b_boot_dir
            ),
        )

        # ------ mock otaclient cfg ------ #
        _cfg = BaseConfig()
        _cfg.MOUNT_POINT = str(self.slot_b)  # type: ignore
        _cfg.ACTIVE_ROOT_MOUNT_POINT = str(self.slot_a)  # type: ignore
        _cfg.RUN_DIR = str(self.otaclient_run_dir)  # type: ignore
        mocker.patch(f"{cfg.OTACLIENT_MODULE_PATH}.cfg", _cfg)
        mocker.patch(f"{MODULE}.rebuild_mode.cfg", _cfg)

    def test_ota_update(
        self, mocker: MockerFixture, msg_queue, stats_monitor: OTAClientStatsCollector
    ):
        # ------ execution ------ #
        otaclient_control_flags = typing.cast(
            OTAClientControlFlags, mocker.MagicMock(spec=OTAClientControlFlags)
        )
        _updater = _OTAUpdater(
            version=cfg.UPDATE_VERSION,
            raw_url_base=cfg.OTA_IMAGE_URL,
            cookies_json=r'{"test": "my-cookie"}',
            upper_otaproxy=None,
            boot_controller=self._boot_control,
            create_standby_cls=RebuildMode,
            control_flags=otaclient_control_flags,
            stats_report_queue=msg_queue,
            session_id=self.SESSION_ID,
        )
        _updater._process_persistents = persist_handler = mocker.MagicMock()

        _updater.execute()
        time.sleep(2)  # wait for downloader to record stats

        # ------ assertions ------ #
        persist_handler.assert_called_once()
        # --- assert update finished
        otaclient_control_flags.wait_can_reboot_flag.assert_called_once()  # type: ignore

        # --- ensure the update stats are collected
        otaclient_status = stats_monitor.otaclient_status
        assert otaclient_status is not None
        assert otaclient_status.session_id == self.SESSION_ID
        assert otaclient_status.update_progress.processed_files_num
        assert otaclient_status.update_progress.processed_files_size
        assert otaclient_status.update_progress.downloaded_files_num
        assert otaclient_status.update_progress.downloaded_files_size
        assert otaclient_status.update_timing.download_start_timestamp
        assert otaclient_status.update_timing.delta_generate_start_timestamp
        assert otaclient_status.update_timing.update_apply_start_timestamp

        # --- check slot creating result, ensure slot_a and slot_b is the same --- #
        # NOTE: merge contents from slot_b_boot_dir to slot_b
        shutil.copytree(self.slot_b_boot_dir, self.slot_b / "boot", dirs_exist_ok=True)
        # NOTE: for some reason tmp dir is created under OTA_IMAGE_DIR/data, but not listed
        # in the regulars.txt, so we create one here to make the test passed
        (self.slot_b / "tmp").mkdir(exist_ok=True)
        # NOTE: remove the ota-meta dir and ota-tmp dir to resolve the difference with OTA image
        shutil.rmtree(
            self.slot_b / Path(otaclient_cfg.OTA_TMP_META_STORE).relative_to("/"),
            ignore_errors=True,
        )
        shutil.rmtree(
            self.slot_b / Path(otaclient_cfg.OTA_TMP_STORE).relative_to("/"),
            ignore_errors=True,
        )
        shutil.rmtree(self.slot_b / "opt/ota", ignore_errors=True)
        # --- check standby slot, ensure it is correctly populated
        compare_dir(Path(cfg.OTA_IMAGE_DIR) / "data", self.slot_b)
