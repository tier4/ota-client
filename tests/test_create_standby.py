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
import pytest
from pathlib import Path
from pytest_mock import MockerFixture

from otaclient.app.create_standby.interface import UpdateMeta
from otaclient.app.create_standby.legacy_mode import LegacyMode
from otaclient.app.create_standby.rebuild_mode import RebuildMode
from otaclient.app.downloader import Downloader
from otaclient.app.ota_metadata import ParseMetadataHelper
from otaclient.app.proto import wrapper
from otaclient.app.update_stats import OTAUpdateStatsCollector

from tests.conftest import TestConfiguration as cfg
from tests.utils import SlotMeta, compare_dir

import logging

logger = logging.getLogger(__name__)


class _Common:
    @pytest.fixture
    def prepare_ab_slots(self, ab_slots: SlotMeta):
        self.slot_a = Path(ab_slots.slot_a)
        self.slot_b = Path(ab_slots.slot_b)
        self.slot_a_boot_dir = Path(ab_slots.slot_a_boot_dev) / "boot"
        self.slot_b_boot_dir = Path(ab_slots.slot_b_boot_dev) / "boot"

        # cleanup slot_b
        shutil.rmtree(self.slot_b, ignore_errors=True)
        self.slot_b.mkdir(exist_ok=True)
        yield
        # cleanup slot_b after test
        shutil.rmtree(self.slot_b, ignore_errors=True)

    @pytest.fixture(autouse=True)
    def update_stats_collector(self):
        _collector = OTAUpdateStatsCollector()
        try:
            self._collector = _collector
            _collector.start()
            yield
        finally:
            _collector.stop()

    @pytest.fixture(autouse=True)
    def prepare_downloader(self):
        try:
            self._downloader = Downloader()
            yield
        finally:
            self._downloader.shutdown()

    @pytest.fixture
    def prepare_certsdir(self):
        _test_dir = Path(__file__).parent
        self.certs_dir = str(_test_dir / "keys")


class Test_RebuildMode(_Common):
    @pytest.fixture(autouse=True)
    def prepare_mock(self, mocker: MockerFixture, prepare_certsdir, prepare_ab_slots):
        cfg_path = f"{cfg.CREATE_STANDBY_MODULE_PATH}.rebuild_mode.cfg"
        proxy_cfg_path = f"{cfg.CREATE_STANDBY_MODULE_PATH}.rebuild_mode.proxy_cfg"
        mocker.patch(f"{cfg_path}.PASSWD_FILE", f"{self.slot_a}/etc/passwd")
        mocker.patch(f"{cfg_path}.GROUP_FILE", f"{self.slot_a}/group")
        mocker.patch(f"{proxy_cfg_path}.get_proxy_for_local_ota", return_value=None)

        # mock RebuildMode
        # TODO: mock save_meta here as save_meta will
        # introduce diff between ota_image and slot b
        rebuild_mode_cls = f"{cfg.CREATE_STANDBY_MODULE_PATH}.rebuild_mode.RebuildMode"
        mocker.patch(f"{rebuild_mode_cls}._save_meta")
        # TODO: mock process_persistents here
        mocker.patch(f"{rebuild_mode_cls}._process_persistents")

        # prepare update meta
        _parser = ParseMetadataHelper(
            (Path(cfg.OTA_IMAGE_DIR) / "metadata.jwt").read_text(),
            certs_dir=self.certs_dir,
        )
        ota_meta = _parser.get_otametadata()
        self.update_meta = UpdateMeta(
            cookies={},
            metadata=ota_meta,
            url_base=f"http://{cfg.OTA_IMAGE_SERVER_ADDR}:{cfg.OTA_IMAGE_SERVER_PORT}",
            boot_dir=str(self.slot_b_boot_dir),
            standby_slot_mount_point=str(self.slot_b),
            ref_slot_mount_point=str(self.slot_a),
        )

    def test_rebuild_mode(self, mocker: MockerFixture):
        update_phase_tracker = mocker.MagicMock()

        builder = RebuildMode(
            update_meta=self.update_meta,
            stats_collector=self._collector,
            update_phase_tracker=update_phase_tracker,
            downloader=self._downloader,
        )
        builder.create_standby_slot()

        # check status progress update
        update_phase_tracker.assert_any_call(wrapper.StatusProgressPhase.METADATA)
        update_phase_tracker.assert_any_call(wrapper.StatusProgressPhase.REGULAR)
        update_phase_tracker.assert_any_call(wrapper.StatusProgressPhase.DIRECTORY)
        # update_phase_tracker.assert_any_call(wrapper.StatusProgressPhase.PERSISTENT)

        # check collected update stats
        _snapshot = self._collector.get_snapshot()
        assert _snapshot.total_regular_files > 0
        assert _snapshot.regular_files_processed > 0
        assert _snapshot.file_size_processed_copy > 0
        assert _snapshot.file_size_processed_download > 0
        assert _snapshot.file_size_processed_link > 0

        # check slot populating result
        # NOTE: merge contents from slot_b_boot_dir to slot_b
        shutil.copytree(self.slot_b_boot_dir, self.slot_b / "boot", dirs_exist_ok=True)
        # NOTE: for some reason tmp dir is created under OTA_IMAGE_DIR/data, but not listed
        # in the regulars.txt, so we create one here to make the test passed
        (self.slot_b / "tmp").mkdir(exist_ok=True)
        compare_dir(Path(cfg.OTA_IMAGE_DIR) / "data", self.slot_b)


class Test_LegacyMode(_Common):
    @pytest.fixture(autouse=True)
    def prepare_mock(self, mocker: MockerFixture, prepare_certsdir, prepare_ab_slots):
        module_root = f"{cfg.CREATE_STANDBY_MODULE_PATH}.legacy_mode"

        cfg_path = f"{module_root}.cfg"
        proxy_cfg_path = f"{module_root}.proxy_cfg"
        mocker.patch(f"{cfg_path}.PASSWD_FILE", f"{self.slot_a}/etc/passwd")
        mocker.patch(f"{cfg_path}.GROUP_FILE", f"{self.slot_a}/group")
        mocker.patch(f"{proxy_cfg_path}.get_proxy_for_local_ota", return_value=None)

        # mock RebuildMode
        # TODO: mock save_meta here as save_meta will
        # introduce diff between ota_image and slot b
        legacy_mode_cls = f"{module_root}.LegacyMode"
        # TODO: mock process_persistents here
        mocker.patch(f"{legacy_mode_cls}._process_persistent")

        # prepare update meta
        _parser = ParseMetadataHelper(
            (Path(cfg.OTA_IMAGE_DIR) / "metadata.jwt").read_text(),
            certs_dir=self.certs_dir,
        )
        ota_meta = _parser.get_otametadata()
        self.update_meta = UpdateMeta(
            cookies={},
            metadata=ota_meta,
            url_base=f"http://{cfg.OTA_IMAGE_SERVER_ADDR}:{cfg.OTA_IMAGE_SERVER_PORT}",
            boot_dir=str(self.slot_b_boot_dir),
            standby_slot_mount_point=str(self.slot_b),
            ref_slot_mount_point=str(self.slot_a),
        )

    def test_legacy_mode(self, mocker: MockerFixture):
        update_phase_tracker = mocker.MagicMock()

        builder = LegacyMode(
            update_meta=self.update_meta,
            stats_collector=self._collector,
            update_phase_tracker=update_phase_tracker,
            downloader=self._downloader,
        )
        builder.create_standby_slot()

        # check status progress update
        # update_phase_tracker.assert_any_call(wrapper.StatusProgressPhase.METADATA)
        update_phase_tracker.assert_any_call(wrapper.StatusProgressPhase.REGULAR)
        update_phase_tracker.assert_any_call(wrapper.StatusProgressPhase.DIRECTORY)
        # update_phase_tracker.assert_any_call(wrapper.StatusProgressPhase.PERSISTENT)

        # check collected update stats
        _snapshot = self._collector.get_snapshot()
        assert _snapshot.total_regular_files > 0
        assert _snapshot.regular_files_processed > 0
        assert _snapshot.file_size_processed_copy > 0
        assert _snapshot.file_size_processed_download > 0
        assert _snapshot.file_size_processed_link > 0

        # check slot populating result
        # NOTE: merge contents from slot_b_boot_dir to slot_b
        shutil.copytree(self.slot_b_boot_dir, self.slot_b / "boot", dirs_exist_ok=True)
        # NOTE: for some reason tmp dir is created under OTA_IMAGE_DIR/data, but not listed
        # in the regulars.txt, so we create one here to make the test passed
        (self.slot_b / "tmp").mkdir(exist_ok=True)
        compare_dir(Path(cfg.OTA_IMAGE_DIR) / "data", self.slot_b)
