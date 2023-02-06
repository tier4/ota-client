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
                _entry = wrapper.parse_regulars_from_txt(_l)
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
            for _dir in map(wrapper.parse_dirs_from_txt, _f):
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

        # ------ mock proxy info ------ #
        # configure not to use proxy for this test
        _proxy_cfg = typing.cast(ProxyInfo, mocker.MagicMock(spec=ProxyInfo))
        _proxy_cfg.enable_local_ota_proxy = False
        _proxy_cfg.get_proxy_for_local_ota.return_value = None
        mocker.patch(f"{cfg.OTACLIENT_MODULE_PATH}.proxy_cfg", _proxy_cfg)

        # ------ mock otaclient cfg ------ #
        _cfg = BaseConfig()
        _cfg.MOUNT_POINT = str(self.slot_b)  # type: ignore
        _cfg.ACTIVE_ROOTFS_PATH = str(self.slot_a)  # type: ignore
        mocker.patch(f"{cfg.OTACLIENT_MODULE_PATH}.cfg", _cfg)

        # ------ mock stats collector ------ #
        mocker.patch(
            f"{cfg.OTACLIENT_MODULE_PATH}.OTAUpdateStatsCollector", mocker.MagicMock()
        )

    def test_OTAUpdater(self, mocker: pytest_mock.MockerFixture):
        from otaclient.app.ota_client import _OTAUpdater, OTAUpdateFSM

        # ------ execution ------ #
        _ota_update_fsm = typing.cast(OTAUpdateFSM, mocker.MagicMock(spec=OTAUpdateFSM))
        _updater = _OTAUpdater(
            self._boot_control,
            create_standby_cls=self._create_standby_cls,
        )

        _updater.execute(
            version=cfg.UPDATE_VERSION,
            raw_url_base=cfg.OTA_IMAGE_URL,
            cookies_json=r'{"test": "my-cookie"}',
            fsm=_ota_update_fsm,
        )

        # ------ assertions ------ #
        # assert ota metefiles are downloaded
        # TODO: check each ota meta files?
        assert list(self.ota_metafiles_tmp_dir.glob("*"))
        # assert OTA files are downloaded
        _downloaded_files_size = 0
        for _f in self.ota_tmp_dir.glob("*"):
            _downloaded_files_size += _f.stat().st_size
        assert _downloaded_files_size == self._delta_bundle.total_download_files_size
        # assert the local update is finished
        _ota_update_fsm.client_finish_update.assert_called_once()
        _ota_update_fsm.client_wait_for_reboot.assert_called_once()
        assert _updater.updating_version == cfg.UPDATE_VERSION
        # assert boot controller is used
        self._boot_control.pre_update.assert_called_once()
        self._boot_control.post_update.assert_called_once()
        # assert create standby module is used
        self._create_standby.calculate_and_prepare_delta.assert_called_once()
        self._create_standby.create_standby_slot.assert_called_once()


class Test_OTAClient:
    """Full OTA update testing on otaclient workflow."""

    MOCKED_STATUS_PROGRESS = wrapper.StatusProgress(
        files_processed_copy=256,
        file_size_processed_download=128,
        file_size_processed_link=6,
    )
    UPDATE_COOKIES_JSON = r'{"test": "my-cookie"}'
    MY_ECU_ID = "autoware"

    @pytest.fixture(autouse=True)
    def mock_setup(self, tmp_path: Path, mocker: pytest_mock.MockerFixture):
        from otaclient.app.ota_client import _OTAUpdater, OTAUpdateFSM

        ###### mock setup ######
        ### mock boot_control ###
        self._boot_control_mock = typing.cast(
            BootControllerProtocol, mocker.MagicMock(spec=BootControllerProtocol)
        )
        self._boot_control_mock.get_ota_status.return_value = wrapper.StatusOta.SUCCESS
        self._boot_control_mock.load_version.return_value = cfg.CURRENT_VERSION
        ### mock create_standby ###
        self._create_standby_mock = typing.cast(
            StandbySlotCreatorProtocol,
            mocker.MagicMock(spec=StandbySlotCreatorProtocol),
        )
        ### mock updater ###
        self._ota_updater = typing.cast(_OTAUpdater, mocker.MagicMock(spec=_OTAUpdater))
        self._ota_updater.update_progress.return_value = (
            cfg.UPDATE_VERSION,
            self.MOCKED_STATUS_PROGRESS,
        )
        ### mock otaupdate fsm ###
        self._fsm = typing.cast(OTAUpdateFSM, mocker.MagicMock(spec=OTAUpdateFSM))
        ### mocke threading.Lock ###
        _otaclient_lock = threading.Lock()
        self._otaclient_lock = typing.cast(
            threading.Lock, mocker.MagicMock(wraps=_otaclient_lock)
        )

        ###### patch ######
        mocker.patch(
            f"{cfg.OTACLIENT_MODULE_PATH}._OTAUpdater", return_value=self._ota_updater
        )
        mocker.patch(
            f"{cfg.OTACLIENT_MODULE_PATH}.OTAUpdateFSM", return_value=self._fsm
        )
        mocker.patch(
            f"{cfg.OTACLIENT_MODULE_PATH}.threading.Lock",
            return_value=self._otaclient_lock,
        )

    def test_update_normal_finished(self, mocker: pytest_mock.MockerFixture):
        from otaclient.app.ota_client import OTAClient

        _ota_client = OTAClient(
            boot_control_cls=mocker.MagicMock(return_value=self._boot_control_mock),
            create_standby_cls=mocker.MagicMock(retun_value=self._create_standby_mock),
            my_ecu_id=self.MY_ECU_ID,
        )

        _ota_client.update(
            cfg.UPDATE_VERSION,
            cfg.OTA_IMAGE_URL,
            self.UPDATE_COOKIES_JSON,
            fsm=self._fsm,
        )

        ###### assert on update ######
        self._otaclient_lock.acquire.assert_called_once_with(blocking=False)
        self._ota_updater.execute.assert_called_once_with(
            cfg.UPDATE_VERSION,
            cfg.OTA_IMAGE_URL,
            self.UPDATE_COOKIES_JSON,
            fsm=ANY,
        )
        self._otaclient_lock.release.assert_called_once()
        assert (
            _ota_client.live_ota_status.get_ota_status() == wrapper.StatusOta.UPDATING
        )
        assert _ota_client.last_failure is None
        self._fsm.on_otaclient_failed.assert_not_called()

    def test_update_interrupted(self, mocker: pytest_mock.MockerFixture):
        from otaclient.app.ota_client import OTAClient

        _ota_client = OTAClient(
            boot_control_cls=mocker.MagicMock(return_value=self._boot_control_mock),
            create_standby_cls=mocker.MagicMock(retun_value=self._create_standby_mock),
            my_ecu_id=self.MY_ECU_ID,
        )

        ###### inject exception ######
        _error = OTAUpdateError(OTAErrorRecoverable("ota_error"))
        self._ota_updater.execute.side_effect = _error

        _ota_client.update(
            cfg.UPDATE_VERSION,
            cfg.OTA_IMAGE_URL,
            self.UPDATE_COOKIES_JSON,
            fsm=self._fsm,
        )

        ###### assert on update ######
        self._otaclient_lock.acquire.assert_called_once_with(blocking=False)
        self._ota_updater.execute.assert_called_once_with(
            cfg.UPDATE_VERSION,
            cfg.OTA_IMAGE_URL,
            self.UPDATE_COOKIES_JSON,
            fsm=ANY,
        )
        self._otaclient_lock.release.assert_called_once()
        assert _ota_client.live_ota_status.get_ota_status() == wrapper.StatusOta.FAILURE
        assert _ota_client.last_failure_type == wrapper.FailureType.RECOVERABLE
        self._fsm.on_otaclient_failed.assert_called_once()

    def test_rollback(self):
        # TODO
        pass

    def test_status_not_in_update(self, mocker: pytest_mock.MockerFixture):
        from otaclient.app.ota_client import OTAClient

        _ota_client = OTAClient(
            boot_control_cls=mocker.MagicMock(return_value=self._boot_control_mock),
            create_standby_cls=mocker.MagicMock(retun_value=self._create_standby_mock),
            my_ecu_id=self.MY_ECU_ID,
        )

        _status = _ota_client.status()
        assert _status == wrapper.StatusResponseEcu(
            ecu_id=self.MY_ECU_ID,
            result=wrapper.FailureType.NO_FAILURE,
            status=wrapper.Status(
                version=cfg.CURRENT_VERSION,
                status=wrapper.StatusOta.SUCCESS,
            ),
        )

    def test_status_in_update(self, mocker: pytest_mock.MockerFixture):
        from otaclient.app.ota_client import OTAClient

        _ota_client = OTAClient(
            boot_control_cls=mocker.MagicMock(return_value=self._boot_control_mock),
            create_standby_cls=mocker.MagicMock(retun_value=self._create_standby_mock),
            my_ecu_id=self.MY_ECU_ID,
        )

        ### set the ota_status to updating
        _ota_client.live_ota_status.set_ota_status(wrapper.StatusOta.UPDATING)
        _ota_client._update_executor = self._ota_updater

        _status = _ota_client.status()
        assert _status == wrapper.StatusResponseEcu(
            ecu_id=self.MY_ECU_ID,
            result=wrapper.FailureType.NO_FAILURE,
            status=wrapper.Status(
                version=cfg.CURRENT_VERSION,
                status=wrapper.StatusOta.UPDATING,
                progress=self.MOCKED_STATUS_PROGRESS,
            ),
        )
