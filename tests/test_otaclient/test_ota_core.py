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
from collections import OrderedDict
from pathlib import Path
from queue import Queue
from typing import Any, Generator

import pytest
import pytest_mock

from ota_metadata.legacy.parser import parse_dirs_from_txt, parse_regulars_from_txt
from ota_metadata.legacy.types import DirectoryInf, RegularInf
from ota_metadata.utils.cert_store import load_ca_cert_chains
from otaclient import ota_core
from otaclient._status_monitor import (
    TERMINATE_SENTINEL,
    OTAClientStatusCollector,
    OTAStatusChangeReport,
    StatusReport,
)
from otaclient._types import OTAStatus, UpdateRequestV2
from otaclient.boot_control import BootControllerProtocol
from otaclient.configs.cfg import cfg as otaclient_cfg
from otaclient.create_standby import StandbySlotCreatorProtocol
from otaclient.create_standby.common import DeltaBundle, RegularDelta
from otaclient.errors import OTAErrorRecoverable
from otaclient.ota_core import OTAClient, OTAClientControlFlags, _OTAUpdater
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


@pytest.fixture(scope="class")
def ota_status_collector() -> (
    Generator[tuple[OTAClientStatusCollector, Queue[StatusReport]], Any, None]
):
    _report_queue: Queue[StatusReport] = Queue()
    _status_collector = OTAClientStatusCollector(_report_queue)
    _collector_thread = _status_collector.start()

    try:
        yield _status_collector, _report_queue
    finally:
        _report_queue.put_nowait(TERMINATE_SENTINEL)
        _collector_thread.join()


class TestOTAUpdater:
    """
    NOTE: the boot_control and create_standby are mocked, only testing
          the logics directly implemented by OTAUpdater
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

    @pytest.fixture
    def _delta_generate(self, prepare_ab_slots):
        _ota_image_dir = Path(cfg.OTA_IMAGE_DIR)
        _standby_ota_tmp = self.slot_b / ".ota-tmp"

        # ------ manually create delta bundle ------ #
        # --- parse regulars.txt --- #
        # NOTE: since we don't prepare any local copy in the test,
        #       we need to download all the unique files
        _donwload_list_dict: dict[bytes, RegularInf] = {}
        _new_delta = RegularDelta()
        _total_regulars_num, _total_donwload_files_size = 0, 0
        with open(_ota_image_dir / "regulars.txt", "r") as _f:
            for _l in _f:
                _entry = parse_regulars_from_txt(_l)
                _total_regulars_num += 1
                _new_delta.add_entry(_entry)
                if _entry.sha256hash not in _donwload_list_dict:
                    _donwload_list_dict[_entry.sha256hash] = _entry
        _download_list = list(_donwload_list_dict.values())
        for _unique_entry in _download_list:
            _total_donwload_files_size += (
                _unique_entry.size if _unique_entry.size else 0
            )

        # --- parse dirs.txt --- #
        _new_dirs: dict[DirectoryInf, None] = OrderedDict()
        with open(_ota_image_dir / "dirs.txt", "r") as _f:
            for _dir in map(parse_dirs_from_txt, _f):
                _new_dirs[_dir] = None

        # --- create bundle --- #
        self._delta_bundle = DeltaBundle(
            rm_delta=[],
            download_list=_download_list,
            new_delta=_new_delta,
            new_dirs=_new_dirs,
            delta_src=self.slot_a,
            delta_files_dir=_standby_ota_tmp,
            total_regular_num=_total_regulars_num,
            total_download_files_size=_total_donwload_files_size,
        )

    @pytest.fixture(autouse=True)
    def mock_setup(self, mocker: pytest_mock.MockerFixture, _delta_generate):
        # ------ mock boot_controller ------ #
        self._boot_control = mocker.MagicMock(spec=BootControllerProtocol)

        # ------ mock create_standby ------ #
        self._create_standby = mocker.MagicMock(spec=StandbySlotCreatorProtocol)
        self._create_standby_cls = mocker.MagicMock(return_value=self._create_standby)
        # NOTE: here we use a pre_calculated mocked delta bundle
        self._create_standby.calculate_and_prepare_delta = mocker.MagicMock(
            spec=StandbySlotCreatorProtocol.calculate_and_prepare_delta,
            return_value=self._delta_bundle,
        )
        self._create_standby.should_erase_standby_slot.return_value = False  # type: ignore

        # ------ mock otaclient cfg ------ #
        mocker.patch(f"{OTA_CORE_MODULE}.cfg.ACTIVE_SLOT_MNT", str(self.slot_a))
        mocker.patch(f"{OTA_CORE_MODULE}.cfg.STANDBY_SLOT_MNT", str(self.slot_b))
        mocker.patch(f"{OTA_CORE_MODULE}.cfg.RUN_DIR", str(self.otaclient_run_dir))

    def test_otaupdater(
        self,
        ota_status_collector: tuple[OTAClientStatusCollector, Queue[StatusReport]],
        mocker: pytest_mock.MockerFixture,
    ):
        from otaclient.ota_core import OTAClientControlFlags, _OTAUpdater

        _, report_queue = ota_status_collector

        # ------ execution ------ #
        otaclient_control_flags = mocker.MagicMock(spec=OTAClientControlFlags)
        otaclient_control_flags._can_reboot = _can_reboot = mocker.MagicMock()
        _can_reboot.is_set = mocker.MagicMock(return_value=True)

        ca_store = load_ca_cert_chains(cfg.CERTS_DIR)

        _updater = _OTAUpdater(
            version=cfg.UPDATE_VERSION,
            raw_url_base=cfg.OTA_IMAGE_URL,
            cookies_json=r'{"test": "my-cookie"}',
            ca_chains_store=ca_store,
            boot_controller=self._boot_control,
            upper_otaproxy=None,
            create_standby_cls=self._create_standby_cls,
            control_flags=otaclient_control_flags,
            session_id=self.SESSION_ID,
            status_report_queue=report_queue,
        )
        _updater._process_persistents = process_persists_handler = mocker.MagicMock()

        # update OTA status to update and assign session_id before execution
        report_queue.put_nowait(
            StatusReport(
                payload=OTAStatusChangeReport(
                    new_ota_status=OTAStatus.UPDATING,
                ),
                session_id=self.SESSION_ID,
            )
        )
        _updater.execute()

        # ------ assertions ------ #
        # assert OTA files are downloaded
        _downloaded_files_size = 0
        for _f in self.ota_tmp_dir.glob("*"):
            _downloaded_files_size += _f.stat().st_size
        assert _downloaded_files_size == self._delta_bundle.total_download_files_size

        # assert the control_flags has been waited
        otaclient_control_flags._can_reboot.is_set.assert_called_once()

        assert _updater.update_version == str(cfg.UPDATE_VERSION)

        # assert boot controller is used
        self._boot_control.pre_update.assert_called_once()
        self._boot_control.post_update.assert_called_once()

        # assert create standby module is used
        self._create_standby.calculate_and_prepare_delta.assert_called_once()
        self._create_standby.create_standby_slot.assert_called_once()
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

        # --- mock setup --- #
        self.control_flags = mocker.MagicMock(spec=OTAClientControlFlags)
        self.ota_updater = mocker.MagicMock(spec=_OTAUpdater)

        self.boot_controller = mocker.MagicMock(spec=BootControllerProtocol)

        # patch boot_controller for otaclient initializing
        self.boot_controller.load_version.return_value = self.CURRENT_FIRMWARE_VERSION
        self.boot_controller.get_booted_ota_status.return_value = OTAStatus.SUCCESS

        # patch inject mocked updater
        mocker.patch(f"{OTA_CORE_MODULE}._OTAUpdater", return_value=self.ota_updater)
        mocker.patch(
            f"{OTA_CORE_MODULE}.get_boot_controller", return_value=self.boot_controller
        )

        # start otaclient
        self.ota_client = OTAClient(
            control_flags=self.control_flags,
            status_report_queue=status_report_queue,
        )

    def test_update_normal_finished(self):
        # --- execution --- #
        self.ota_client.update(
            request=UpdateRequestV2(
                version=self.UPDATE_FIRMWARE_VERSION,
                url_base=self.OTA_IMAGE_URL,
                cookies_json=self.UPDATE_COOKIES_JSON,
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
            )
        )

        # --- assertion on interrupted OTA update --- #
        self.ota_updater.execute.assert_called_once()
        assert self.ota_client.live_ota_status == OTAStatus.FAILURE

    def test_status_in_update(self, mocker: pytest_mock.MockerFixture):
        # --- mock setup --- #
        _ota_updater_mocker = mocker.MagicMock(spec=_OTAUpdater)
        mocker.patch(f"{OTA_CORE_MODULE}._OTAUpdater", _ota_updater_mocker)
        self.ota_client._live_ota_status = OTAStatus.UPDATING

        # --- execution --- #
        self.ota_client.update(
            request=UpdateRequestV2(
                version=self.UPDATE_FIRMWARE_VERSION,
                url_base=self.OTA_IMAGE_URL,
                cookies_json=self.UPDATE_COOKIES_JSON,
            )
        )

        # --- assertion --- #
        # confirm that the OTA update doesn't happen
        _ota_updater_mocker.assert_not_called()
