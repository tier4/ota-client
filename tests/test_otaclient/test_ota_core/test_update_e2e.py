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

import shutil
from pathlib import Path
from queue import Queue

import pytest
import pytest_mock

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
from otaclient.metrics import OTAMetricsData
from otaclient.ota_core import OTAUpdaterForLegacyOTAImage
from otaclient.ota_core._common import create_downloader_pool
from tests.conftest import TestConfiguration as cfg
from tests.utils import SlotMeta

OTA_UPDATER_MODULE = ota_core._updater.__name__


class TestOTAUpdater:
    """
    NOTE: the boot_control is mocked.
    """

    SESSION_ID = "session_id_for_test"

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
            otaclient_cfg.OTA_META_STORE
        ).relative_to("/")
        self.ota_tmp_dir = self.slot_b / Path(
            otaclient_cfg.OTA_RESOURCES_STORE
        ).relative_to("/")

        yield
        # cleanup slot_b after test
        shutil.rmtree(self.slot_b, ignore_errors=True)

    @pytest.fixture(autouse=True)
    def mock_setup(self, mocker: pytest_mock.MockerFixture, prepare_ab_slots):
        # ------ mock boot_controller ------ #
        self._boot_control = _boot_control_mock = mocker.MagicMock(
            spec=BootControllerProtocol
        )
        _boot_control_mock.get_standby_slot_path.return_value = self.slot_b

        # ------ mock otaclient cfg ------ #
        mocker.patch(f"{OTA_UPDATER_MODULE}.cfg.ACTIVE_SLOT_MNT", str(self.slot_a))
        mocker.patch(f"{OTA_UPDATER_MODULE}.cfg.STANDBY_SLOT_MNT", str(self.slot_b))
        mocker.patch(f"{OTA_UPDATER_MODULE}.cfg.RUN_DIR", str(self.otaclient_run_dir))
        mocker.patch(f"{OTA_UPDATER_MODULE}.can_use_in_place_mode", return_value=False)

        self._process_persists_mock = process_persists_mock = mocker.MagicMock()
        mocker.patch(f"{OTA_UPDATER_MODULE}.process_persistents", process_persists_mock)

    def test_ota_updater(
        self,
        ota_status_collector: tuple[OTAClientStatusCollector, Queue[StatusReport]],
        mocker: pytest_mock.MockerFixture,
        tmp_path: Path,
    ) -> None:
        _, report_queue = ota_status_collector
        ecu_status_flags = mocker.MagicMock()
        ecu_status_flags.any_child_ecu_in_update.is_set = mocker.MagicMock(
            return_value=False
        )
        critical_zone_flag = mocker.MagicMock()

        # ------ execution ------ #
        ca_chains_store = load_ca_cert_chains(cfg.CERTS_DIR)
        downloader_pool = create_downloader_pool(
            raw_cookies_json=cfg.COOKIES_JSON,
            download_threads=3,
            chunk_size=1024**2,
        )

        # update OTA status to update and assign session_id before execution
        report_queue.put_nowait(
            StatusReport(
                payload=OTAStatusChangeReport(
                    new_ota_status=OTAStatus.UPDATING,
                ),
                session_id=self.SESSION_ID,
            )
        )

        session_workdir = tmp_path / "session_workdir"
        _updater = OTAUpdaterForLegacyOTAImage(
            version=cfg.UPDATE_VERSION,
            raw_url_base=cfg.OTA_IMAGE_URL,
            session_wd=session_workdir,
            ca_chains_store=ca_chains_store,
            downloader_pool=downloader_pool,
            boot_controller=self._boot_control,
            ecu_status_flags=ecu_status_flags,
            critical_zone_flag=critical_zone_flag,
            session_id=self.SESSION_ID,
            status_report_queue=report_queue,
            metrics=OTAMetricsData(),
            shm_metrics_reader=None,  # type: ignore
        )

        _updater.execute()

        # ------ assertions ------ #
        # assert the control_flags has been waited
        ecu_status_flags.any_child_ecu_in_update.is_set.assert_called_once()

        assert _updater.update_version == str(cfg.UPDATE_VERSION)

        self._boot_control.pre_update.assert_called_once()
        self._boot_control.post_update.assert_called_once()
        self._process_persists_mock.assert_called_once()
