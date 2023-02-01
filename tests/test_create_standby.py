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
import typing
import pytest
from pathlib import Path
from pytest_mock import MockerFixture

from otaclient.app.boot_control import BootControllerProtocol
from otaclient.app.configs import config as otaclient_cfg
from otaclient.app.proto import wrapper

from tests.conftest import TestConfiguration as cfg
from tests.utils import SlotMeta, compare_dir

import logging

logger = logging.getLogger(__name__)


class Test_OTAupdate_with_create_standby_RebuildMode:
    """
    NOTE: the boot_control is mocked, only testing
          create_standby and the logics directly implemented by OTAUpdater
    """

    @pytest.fixture
    def prepare_ab_slots(self, ab_slots: SlotMeta):
        self.slot_a = Path(ab_slots.slot_a)
        self.slot_b = Path(ab_slots.slot_b)
        self.slot_a_boot_dir = Path(ab_slots.slot_a_boot_dev) / "boot"
        self.slot_b_boot_dir = Path(ab_slots.slot_b_boot_dev) / "boot"
        self.ota_image_dir = Path(cfg.OTA_IMAGE_DIR)

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
        from otaclient.app.proxy_info import ProxyInfo
        from otaclient.app.configs import BaseConfig

        # ------ mock boot_controller ------ #
        self._boot_control = typing.cast(
            BootControllerProtocol, mocker.MagicMock(spec=BootControllerProtocol)
        )
        self._boot_control.get_standby_boot_dir.return_value = self.slot_b_boot_dir

        # ------ mock proxy info ------ #
        # configure not to use proxy for this test
        _proxy_cfg = typing.cast(ProxyInfo, mocker.MagicMock(spec=ProxyInfo))
        _proxy_cfg.enable_local_ota_proxy = False
        _proxy_cfg.get_proxy_for_local_ota.return_value = None
        mocker.patch(f"{cfg.OTACLIENT_MODULE_PATH}.proxy_cfg", _proxy_cfg)

        # ------ mock otaclient cfg ------ #
        _cfg = BaseConfig()
        _cfg.MOUNT_POINT = str(self.slot_b)  # type: ignore
        _cfg.ACTIVE_ROOT_MOUNT_POINT = str(self.slot_a)  # type: ignore
        mocker.patch(f"{cfg.OTACLIENT_MODULE_PATH}.cfg", _cfg)
        mocker.patch(f"{cfg.CREATE_STANDBY_MODULE_PATH}.rebuild_mode.cfg", _cfg)

    def test_update_with_create_standby_RebuildMode(self, mocker: MockerFixture):
        from otaclient.app.ota_client import _OTAUpdater, OTAUpdateFSM
        from otaclient.app.create_standby.rebuild_mode import RebuildMode

        # TODO: not test process_persistent currently,
        #       as we currently directly compare the standby slot
        #       with the OTA image.
        RebuildMode._process_persistents = mocker.MagicMock()

        # ------ execution ------ #
        _ota_update_fsm = typing.cast(OTAUpdateFSM, mocker.MagicMock(spec=OTAUpdateFSM))
        _updater = _OTAUpdater(
            self._boot_control,
            create_standby_cls=RebuildMode,
        )
        # NOTE: mock the shutdown method as we need to assert before the
        #       updater is closed.
        _updater._set_update_phase = mocker.MagicMock()
        _updater_shutdown = _updater.shutdown
        _updater.shutdown = mocker.MagicMock()

        _updater.execute(
            version=cfg.UPDATE_VERSION,
            raw_url_base=cfg.OTA_IMAGE_URL,
            cookies_json=r'{"test": "my-cookie"}',
            fsm=_ota_update_fsm,
        )

        # ------ assertions ------ #
        # --- assert update finished
        _updater.shutdown.assert_called_once()
        # --- assert each update phases are finished
        _updater._set_update_phase.assert_any_call(wrapper.StatusProgressPhase.REGULAR)
        _updater._set_update_phase.assert_any_call(
            wrapper.StatusProgressPhase.DIRECTORY
        )
        # TODO: not test process_persistent currently
        # _updater._set_update_phase.assert_any_call(
        #     wrapper.StatusProgressPhase.PERSISTENT
        # )
        # --- ensure the update stats are collected
        _snapshot = _updater._update_stats_collector.get_snapshot()
        assert _snapshot.total_regular_files > 0
        assert _snapshot.regular_files_processed > 0
        assert _snapshot.file_size_processed_copy > 0
        assert _snapshot.file_size_processed_download > 0
        assert _snapshot.file_size_processed_link > 0
        assert _snapshot.download_bytes > 0
        # --- check slot creating result
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
        compare_dir(Path(cfg.OTA_IMAGE_DIR) / "data", self.slot_b)

        # ------ finally close the updater ------ #
        _updater_shutdown()
