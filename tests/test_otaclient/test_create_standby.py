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
import typing
from pathlib import Path
from queue import Queue

import pytest
from pytest_mock import MockerFixture

from ota_metadata.utils.cert_store import load_ca_cert_chains
from otaclient import ota_core
from otaclient._status_monitor import (
    OTAClientStatusCollector,
    OTAStatusChangeReport,
    StatusReport,
)
from otaclient._types import OTAStatus
from otaclient.boot_control import BootControllerProtocol
from otaclient.configs.cfg import cfg as otaclient_cfg
from otaclient.create_standby import common, rebuild_mode
from otaclient.create_standby.rebuild_mode import RebuildMode
from otaclient.ota_core import OTAClientControlFlags, _OTAUpdater
from tests.conftest import TestConfiguration as cfg
from tests.utils import SlotMeta, compare_dir

logger = logging.getLogger(__name__)

REBUILD_MODE_MODULE = rebuild_mode.__name__
COMMON_MODULE = common.__name__
OTA_CORE_MODULE = ota_core.__name__


class TestOTAupdateWithCreateStandbyRebuildMode:
    """
    NOTE: the boot_control is mocked, only testing
          create_standby and the logics directly implemented by OTAUpdater
    """

    SESSION_ID = "session_id"

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
            BootControllerProtocol, mocker.MagicMock(spec=BootControllerProtocol)
        )
        self._boot_control.get_standby_boot_dir.return_value = self.slot_b_boot_dir  # type: ignore

        # ------ mock otaclient cfg ------ #
        mocker.patch(f"{OTA_CORE_MODULE}.cfg.STANDBY_SLOT_MNT", str(self.slot_b))
        mocker.patch(f"{OTA_CORE_MODULE}.cfg.ACTIVE_SLOT_MNT", str(self.slot_a))
        mocker.patch(f"{OTA_CORE_MODULE}.cfg.RUN_DIR", str(self.otaclient_run_dir))

        mocker.patch(f"{REBUILD_MODE_MODULE}.cfg.STANDBY_SLOT_MNT", str(self.slot_b))
        mocker.patch(f"{REBUILD_MODE_MODULE}.cfg.ACTIVE_SLOT_MNT", str(self.slot_a))
        mocker.patch(f"{REBUILD_MODE_MODULE}.cfg.RUN_DIR", str(self.otaclient_run_dir))

    def test_update_with_rebuild_mode(
        self,
        ota_status_collector: tuple[OTAClientStatusCollector, Queue[StatusReport]],
        mocker: MockerFixture,
    ):
        status_collector, status_report_queue = ota_status_collector

        # ------ execution ------ #
        otaclient_control_flags = typing.cast(
            OTAClientControlFlags, mocker.MagicMock(spec=OTAClientControlFlags)
        )
        otaclient_control_flags._can_reboot = _can_reboot = mocker.MagicMock()
        _can_reboot.is_set = mocker.MagicMock(return_value=True)

        ca_store = load_ca_cert_chains(cfg.CERTS_DIR)

        _updater = _OTAUpdater(
            version=cfg.UPDATE_VERSION,
            raw_url_base=cfg.OTA_IMAGE_URL,
            cookies_json=r'{"test": "my-cookie"}',
            ca_chains_store=ca_store,
            upper_otaproxy=None,
            boot_controller=self._boot_control,
            create_standby_cls=RebuildMode,
            control_flags=otaclient_control_flags,
            status_report_queue=status_report_queue,
            session_id=self.SESSION_ID,
        )
        _updater._process_persistents = persist_handler = mocker.MagicMock()

        # update OTA status to update and assign session_id before execution
        status_report_queue.put_nowait(
            StatusReport(
                payload=OTAStatusChangeReport(
                    new_ota_status=OTAStatus.UPDATING,
                ),
                session_id=self.SESSION_ID,
            )
        )
        _updater.execute()
        time.sleep(2)  # wait for downloader to record stats

        # ------ assertions ------ #
        persist_handler.assert_called_once()

        otaclient_control_flags._can_reboot.is_set.assert_called_once()
        # --- ensure the update stats are collected
        collected_status = status_collector.otaclient_status
        assert collected_status
        assert (_update_progress := collected_status.update_progress)
        assert _update_progress.processed_files_num
        assert _update_progress.processed_files_size
        assert _update_progress.downloaded_files_num
        assert _update_progress.downloaded_files_size

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
