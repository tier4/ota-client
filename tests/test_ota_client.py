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


import threading
import typing
import pytest
import pytest_mock
import shutil
from collections import OrderedDict
from pathlib import Path
from typing import Dict
from unittest.mock import ANY

from otaclient.app.boot_control import BootControllerProtocol
from otaclient.app.create_standby import StandbySlotCreatorProtocol
from otaclient.app.create_standby.common import DeltaBundle, RegularDelta
from otaclient.app.configs import config as otaclient_cfg
from otaclient.app.errors import OTAErrorRecoverable, OTAUpdateError
from otaclient.app.ota_client import OTAClient, _OTAUpdater, OTAClientControlFlags
from otaclient.app.ota_metadata import parse_regulars_from_txt, parse_dirs_from_txt
from otaclient.app.proto.wrapper import RegularInf, DirectoryInf
from otaclient.app.proto import wrapper

from tests.conftest import TestConfiguration as cfg
from tests.utils import SlotMeta


class Test_OTAUpdater:
    """
    NOTE: the boot_control and create_standby are mocked, only testing
          the logics directly implemented by OTAUpdater
    """

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
        _donwload_list_dict: Dict[bytes, RegularInf] = {}
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
        _new_dirs: Dict[DirectoryInf, None] = OrderedDict()
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
        from otaclient.app.proxy_info import ProxyInfo
        from otaclient.app.configs import BaseConfig

        # ------ mock boot_controller ------ #
        self._boot_control = typing.cast(
            BootControllerProtocol, mocker.MagicMock(spec=BootControllerProtocol)
        )
        # ------ mock create_standby ------ #
        self._create_standby = typing.cast(
            StandbySlotCreatorProtocol,
            mocker.MagicMock(spec=StandbySlotCreatorProtocol),
        )
        self._create_standby_cls = mocker.MagicMock(return_value=self._create_standby)
        # NOTE: here we use a pre_calculated mocked delta bundle
        self._create_standby.calculate_and_prepare_delta = mocker.MagicMock(
            spec=StandbySlotCreatorProtocol.calculate_and_prepare_delta,
            return_value=self._delta_bundle,
        )
        self._create_standby.should_erase_standby_slot.return_value = False

        # ------ mock otaclient cfg ------ #
        _cfg = BaseConfig()
        _cfg.MOUNT_POINT = str(self.slot_b)  # type: ignore
        _cfg.ACTIVE_ROOTFS_PATH = str(self.slot_a)  # type: ignore
        _cfg.RUN_DIR = str(self.otaclient_run_dir)  # type: ignore
        mocker.patch(f"{cfg.OTACLIENT_MODULE_PATH}.cfg", _cfg)
        mocker.patch(f"{cfg.OTAMETA_MODULE_PATH}.cfg", _cfg)

        # ------ mock stats collector ------ #
        mocker.patch(
            f"{cfg.OTACLIENT_MODULE_PATH}.OTAUpdateStatsCollector", mocker.MagicMock()
        )

    def test_OTAUpdater(self, mocker: pytest_mock.MockerFixture):
        from otaclient.app.ota_client import _OTAUpdater, OTAClientControlFlags

        # ------ execution ------ #
        otaclient_control_flags = typing.cast(
            OTAClientControlFlags, mocker.MagicMock(spec=OTAClientControlFlags)
        )
        _updater = _OTAUpdater(
            boot_controller=self._boot_control,
            create_standby_cls=self._create_standby_cls,
            proxy=None,
            control_flags=otaclient_control_flags,
        )

        _updater.execute(
            version=cfg.UPDATE_VERSION,
            raw_url_base=cfg.OTA_IMAGE_URL,
            cookies_json=r'{"test": "my-cookie"}',
        )

        # ------ assertions ------ #
        # assert OTA files are downloaded
        _downloaded_files_size = 0
        for _f in self.ota_tmp_dir.glob("*"):
            _downloaded_files_size += _f.stat().st_size
        assert _downloaded_files_size == self._delta_bundle.total_download_files_size
        # assert the control_flags has been waited
        otaclient_control_flags.otaclient_wait_for_reboot.assert_called_once()
        assert _updater.updating_version == cfg.UPDATE_VERSION
        # assert boot controller is used
        self._boot_control.pre_update.assert_called_once()
        self._boot_control.post_update.assert_called_once()
        # assert create standby module is used
        self._create_standby.calculate_and_prepare_delta.assert_called_once()
        self._create_standby.create_standby_slot.assert_called_once()


class Test_OTAClient:
    """Testing on OTAClient workflow."""

    OTACLIENT_VERSION = "otaclient_version"
    CURRENT_FIRMWARE_VERSION = "firmware_version"
    UPDATE_FIRMWARE_VERSION = "update_firmware_version"

    MOCKED_STATUS_PROGRESS = wrapper.UpdateStatus(
        update_firmware_version=UPDATE_FIRMWARE_VERSION,
        downloaded_bytes=456789,
        downloaded_files_num=567,
        downloaded_files_size=25,
        processed_files_num=256,
        processed_files_size=134,
    )
    MOCKED_STATUS_PROGRESS_V1 = MOCKED_STATUS_PROGRESS.convert_to_v1_StatusProgress()

    UPDATE_COOKIES_JSON = r'{"test": "my-cookie"}'
    OTA_IMAGE_URL = "url"
    MY_ECU_ID = "autoware"

    @pytest.fixture(autouse=True)
    def mock_setup(self, mocker: pytest_mock.MockerFixture):
        # --- mock setup --- #
        self.control_flags = typing.cast(
            OTAClientControlFlags, mocker.MagicMock(spec=OTAClientControlFlags)
        )
        # NOTE: threading.Lock is an alias, so we specs it with its instance
        self.ota_lock = typing.cast(
            threading.Lock, mocker.MagicMock(spec=threading.Lock())
        )
        self.ota_updater = typing.cast(_OTAUpdater, mocker.MagicMock(spec=_OTAUpdater))

        self.boot_controller = typing.cast(
            BootControllerProtocol, mocker.MagicMock(spec=BootControllerProtocol)
        )
        # patch boot_controller for otaclient initializing
        self.boot_controller.load_version.return_value = self.CURRENT_FIRMWARE_VERSION
        self.boot_controller.get_ota_status.return_value = wrapper.StatusOta.SUCCESS

        self.ota_client = OTAClient(
            boot_control_cls=mocker.MagicMock(return_value=self.boot_controller),
            create_standby_cls=mocker.MagicMock(),
            my_ecu_id=self.MY_ECU_ID,
            control_flags=self.control_flags,
        )

        # patch inject mocked updater
        mocker.patch(
            f"{cfg.OTACLIENT_MODULE_PATH}._OTAUpdater", return_value=self.ota_updater
        )
        # inject lock into otaclient
        self.ota_client._lock = self.ota_lock
        # inject otaclient version
        self.ota_client.OTACLIENT_VERSION = self.OTACLIENT_VERSION

    def test_update_normal_finished(self):
        # --- execution --- #
        self.ota_client.update(
            self.UPDATE_FIRMWARE_VERSION,
            self.OTA_IMAGE_URL,
            self.UPDATE_COOKIES_JSON,
        )

        # --- assert on update finished(before reboot) --- #
        self.ota_lock.acquire.assert_called_once_with(blocking=False)
        self.ota_updater.execute.assert_called_once_with(
            self.UPDATE_FIRMWARE_VERSION,
            self.OTA_IMAGE_URL,
            self.UPDATE_COOKIES_JSON,
        )
        self.ota_lock.release.assert_called_once()
        assert (
            self.ota_client.live_ota_status.get_ota_status()
            == wrapper.StatusOta.UPDATING
        )

    def test_update_interrupted(self):
        # inject exception
        _error = OTAUpdateError(OTAErrorRecoverable("network disconnected"))
        self.ota_updater.execute.side_effect = _error

        # --- execution --- #
        self.ota_client.update(
            self.UPDATE_FIRMWARE_VERSION,
            self.OTA_IMAGE_URL,
            self.UPDATE_COOKIES_JSON,
        )

        # --- assertion on interrupted OTA update --- #
        self.ota_lock.acquire.assert_called_once_with(blocking=False)
        self.ota_updater.execute.assert_called_once_with(
            self.UPDATE_FIRMWARE_VERSION,
            self.OTA_IMAGE_URL,
            self.UPDATE_COOKIES_JSON,
        )
        self.ota_lock.release.assert_called_once()

        assert (
            self.ota_client.live_ota_status.get_ota_status()
            == wrapper.StatusOta.FAILURE
        )
        assert self.ota_client.last_failure_type == wrapper.FailureType.RECOVERABLE

    def test_rollback(self):
        # TODO
        pass

    def test_status_not_in_update(self):
        # --- query status --- #
        _status = self.ota_client.status()

        # assert v2 to v1 conversion
        assert _status.convert_to_v1() == wrapper.StatusResponseEcu(
            ecu_id=self.MY_ECU_ID,
            result=wrapper.FailureType.NO_FAILURE,
            status=wrapper.Status(
                version=self.CURRENT_FIRMWARE_VERSION,
                status=wrapper.StatusOta.SUCCESS,
            ),
        )
        # assert to v2
        assert _status == wrapper.StatusResponseEcuV2(
            ecu_id=self.MY_ECU_ID,
            otaclient_version=self.OTACLIENT_VERSION,
            firmware_version=self.CURRENT_FIRMWARE_VERSION,
            failure_type=wrapper.FailureType.NO_FAILURE,
            ota_status=wrapper.StatusOta.SUCCESS,
        )

    def test_status_in_update(self):
        # --- mock setup --- #
        # inject ota_updater and set ota_status to UPDATING to simulate ota updating
        self.ota_client._update_executor = self.ota_updater
        self.ota_client.live_ota_status.set_ota_status(wrapper.StatusOta.UPDATING)
        # let mocked updater return mocked_status_progress
        self.ota_updater.get_update_status.return_value = self.MOCKED_STATUS_PROGRESS

        # --- assertion --- #
        _status = self.ota_client.status()
        # test v2 to v1 conversion
        assert _status.convert_to_v1() == wrapper.StatusResponseEcu(
            ecu_id=self.MY_ECU_ID,
            result=wrapper.FailureType.NO_FAILURE,
            status=wrapper.Status(
                version=self.CURRENT_FIRMWARE_VERSION,
                status=wrapper.StatusOta.UPDATING,
                progress=self.MOCKED_STATUS_PROGRESS_V1,
            ),
        )
        # assert to v2
        assert _status == wrapper.StatusResponseEcuV2(
            ecu_id=self.MY_ECU_ID,
            otaclient_version=self.OTACLIENT_VERSION,
            failure_type=wrapper.FailureType.NO_FAILURE,
            ota_status=wrapper.StatusOta.UPDATING,
            firmware_version=self.CURRENT_FIRMWARE_VERSION,
            update_status=self.MOCKED_STATUS_PROGRESS,
        )


class TestOTAClientStub:
    pass
