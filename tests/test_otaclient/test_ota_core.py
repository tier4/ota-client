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
from otaclient._types import OTAStatus, UpdateRequestV2
from otaclient.boot_control import BootControllerProtocol
from otaclient.configs.cfg import cfg as otaclient_cfg
from otaclient.create_standby.rebuild_mode import RebuildMode
from otaclient.errors import OTAErrorRecoverable
from otaclient.ota_core import OTAClient, _OTAUpdater
from tests.conftest import TestConfiguration as cfg
from tests.utils import SlotMeta

OTA_CORE_MODULE = ota_core.__name__


@pytest.fixture(autouse=True, scope="module")
def mock_certs_dir(module_mocker: pytest_mock.MockerFixture):
    """Mock to use the certs from the OTA test base image."""
    from otaclient.ota_core import cfg as _cfg

    module_mocker.patch.object(
        _cfg,
        "CERT_DPATH",
        cfg.CERTS_DIR,
    )


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
            otaclient_cfg.OTA_TMP_META_STORE
        ).relative_to("/")
        self.ota_tmp_dir = self.slot_b / Path(otaclient_cfg.OTA_TMP_STORE).relative_to(
            "/"
        )

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
        mocker.patch(f"{OTA_CORE_MODULE}.cfg.ACTIVE_SLOT_MNT", str(self.slot_a))
        mocker.patch(f"{OTA_CORE_MODULE}.cfg.STANDBY_SLOT_MNT", str(self.slot_b))
        mocker.patch(f"{OTA_CORE_MODULE}.cfg.RUN_DIR", str(self.otaclient_run_dir))

    def test_otaupdater(
        self,
        ota_status_collector: tuple[OTAClientStatusCollector, Queue[StatusReport]],
        mocker: pytest_mock.MockerFixture,
    ) -> None:
        _, report_queue = ota_status_collector
        ecu_status_flags = mocker.MagicMock()
        ecu_status_flags.any_child_ecu_in_update.is_set = mocker.MagicMock(
            return_value=False
        )

        # ------ execution ------ #
        ca_store = load_ca_cert_chains(cfg.CERTS_DIR)

        # update OTA status to update and assign session_id before execution
        report_queue.put_nowait(
            StatusReport(
                payload=OTAStatusChangeReport(
                    new_ota_status=OTAStatus.UPDATING,
                ),
                session_id=self.SESSION_ID,
            )
        )

        _updater = _OTAUpdater(
            version=cfg.UPDATE_VERSION,
            raw_url_base=cfg.OTA_IMAGE_URL,
            cookies_json=r'{"test": "my-cookie"}',
            ca_chains_store=ca_store,
            boot_controller=self._boot_control,
            upper_otaproxy=None,
            create_standby_cls=RebuildMode,
            ecu_status_flags=ecu_status_flags,
            session_id=self.SESSION_ID,
            status_report_queue=report_queue,
        )
        _updater._process_persistents = process_persists_handler = mocker.MagicMock()

        _updater.execute()

        # ------ assertions ------ #
        # assert the control_flags has been waited
        ecu_status_flags.any_child_ecu_in_update.is_set.assert_called_once()

        assert _updater.update_version == str(cfg.UPDATE_VERSION)

        self._boot_control.pre_update.assert_called_once()
        self._boot_control.post_update.assert_called_once()
        process_persists_handler.assert_called_once()


class TestOTAClient:
    """Testing on OTAClient workflow."""

    OTACLIENT_VERSION = "otaclient_version"
    CURRENT_FIRMWARE_VERSION = "firmware_version"
    UPDATE_FIRMWARE_VERSION = "update_firmware_version"

    UPDATE_COOKIES_JSON = r'{"test": "my-cookie"}'
    OTA_IMAGE_URL = "url"
    MY_ECU_ID = "autoware"

    @pytest.fixture(autouse=True)
    def mock_setup(
        self,
        ota_status_collector: tuple[OTAClientStatusCollector, Queue[StatusReport]],
        mocker: pytest_mock.MockerFixture,
    ):
        _, status_report_queue = ota_status_collector
        ecu_status_flags = mocker.MagicMock()
        ecu_status_flags.any_child_ecu_in_update.is_set = mocker.MagicMock(
            return_value=False
        )

        # --- mock setup --- #
        self.control_flags = ecu_status_flags
        self.ota_updater = mocker.MagicMock(spec=_OTAUpdater)

        self.boot_controller = mocker.MagicMock(spec=BootControllerProtocol)

        # patch boot_controller for otaclient initializing
        self.boot_controller.load_version.return_value = self.CURRENT_FIRMWARE_VERSION
        self.boot_controller.get_booted_ota_status = mocker.MagicMock(
            return_value=OTAStatus.SUCCESS
        )

        # patch inject mocked updater
        mocker.patch(f"{OTA_CORE_MODULE}._OTAUpdater", return_value=self.ota_updater)
        mocker.patch(
            f"{OTA_CORE_MODULE}.get_boot_controller", return_value=self.boot_controller
        )

        # start otaclient
        self.ota_client = OTAClient(
            ecu_status_flags=ecu_status_flags,
            status_report_queue=status_report_queue,
        )

    def test_update_normal_finished(self):
        # --- execution --- #
        self.ota_client.update(
            request=UpdateRequestV2(
                version=self.UPDATE_FIRMWARE_VERSION,
                url_base=self.OTA_IMAGE_URL,
                cookies_json=self.UPDATE_COOKIES_JSON,
                session_id="test_update_normal_finished",
            )
        )

        # --- assert on update finished(before reboot) --- #
        self.ota_updater.execute.assert_called_once()
        assert self.ota_client.live_ota_status == OTAStatus.UPDATING

    def test_update_interrupted(self):
        # inject exception
        _error = OTAErrorRecoverable("interrupted by test as expected", module=__name__)
        self.ota_updater.execute.side_effect = _error

        # --- execution --- #
        self.ota_client.update(
            request=UpdateRequestV2(
                version=self.UPDATE_FIRMWARE_VERSION,
                url_base=self.OTA_IMAGE_URL,
                cookies_json=self.UPDATE_COOKIES_JSON,
                session_id="test_updaste_interrupted",
            )
        )

        # --- assertion on interrupted OTA update --- #
        self.ota_updater.execute.assert_called_once()
        assert self.ota_client.live_ota_status == OTAStatus.FAILURE
