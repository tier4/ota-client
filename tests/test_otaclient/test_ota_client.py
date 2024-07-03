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

import asyncio
import shutil
import threading
import typing
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict

import pytest
import pytest_mock

from ota_metadata.legacy.parser import parse_dirs_from_txt, parse_regulars_from_txt
from ota_metadata.legacy.types import DirectoryInf, RegularInf
from otaclient.app.boot_control import BootControllerProtocol
from otaclient.app.boot_control.configs import BootloaderType
from otaclient.app.configs import config as otaclient_cfg
from otaclient.app.create_standby import StandbySlotCreatorProtocol
from otaclient.app.create_standby.common import DeltaBundle, RegularDelta
from otaclient.app.errors import OTAErrorRecoverable
from otaclient.app.ota_client import (
    OTAClient,
    OTAClientControlFlags,
    OTAServicer,
    _OTAUpdater,
)
from otaclient.configs.ecu_info import ECUInfo
from otaclient_api.v2 import types as api_types
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

        # ------ mock stats collector ------ #
        mocker.patch(
            f"{cfg.OTACLIENT_MODULE_PATH}.OTAUpdateStatsCollector", mocker.MagicMock()
        )

    def test_OTAUpdater(self, mocker: pytest_mock.MockerFixture):
        from otaclient.app.ota_client import OTAClientControlFlags, _OTAUpdater

        # ------ execution ------ #
        otaclient_control_flags = typing.cast(
            OTAClientControlFlags, mocker.MagicMock(spec=OTAClientControlFlags)
        )
        _updater = _OTAUpdater(
            version=cfg.UPDATE_VERSION,
            raw_url_base=cfg.OTA_IMAGE_URL,
            cookies_json=r'{"test": "my-cookie"}',
            boot_controller=self._boot_control,
            upper_otaproxy=None,
            create_standby_cls=self._create_standby_cls,
            control_flags=otaclient_control_flags,
        )
        _updater._process_persistents = process_persists_handler = mocker.MagicMock()

        _updater.execute()

        # ------ assertions ------ #
        # assert OTA files are downloaded
        _downloaded_files_size = 0
        for _f in self.ota_tmp_dir.glob("*"):
            _downloaded_files_size += _f.stat().st_size
        assert _downloaded_files_size == self._delta_bundle.total_download_files_size
        # assert the control_flags has been waited
        otaclient_control_flags.wait_can_reboot_flag.assert_called_once()
        assert _updater.updating_version == str(cfg.UPDATE_VERSION)
        # assert boot controller is used
        self._boot_control.pre_update.assert_called_once()
        self._boot_control.post_update.assert_called_once()
        # assert create standby module is used
        self._create_standby.calculate_and_prepare_delta.assert_called_once()
        self._create_standby.create_standby_slot.assert_called_once()
        process_persists_handler.assert_called_once()


class Test_OTAClient:
    """Testing on OTAClient workflow."""

    OTACLIENT_VERSION = "otaclient_version"
    CURRENT_FIRMWARE_VERSION = "firmware_version"
    UPDATE_FIRMWARE_VERSION = "update_firmware_version"

    MOCKED_STATUS_PROGRESS = api_types.UpdateStatus(
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
        self.boot_controller.get_booted_ota_status.return_value = (
            api_types.StatusOta.SUCCESS
        )

        self.ota_client = OTAClient(
            boot_controller=self.boot_controller,
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
        self.ota_updater.execute.assert_called_once()
        self.ota_lock.release.assert_called_once()
        assert (
            self.ota_client.live_ota_status.get_ota_status()
            == api_types.StatusOta.UPDATING
        )

    def test_update_interrupted(self):
        # inject exception
        _error = OTAErrorRecoverable("network disconnected", module=__name__)
        self.ota_updater.execute.side_effect = _error

        # --- execution --- #
        self.ota_client.update(
            self.UPDATE_FIRMWARE_VERSION,
            self.OTA_IMAGE_URL,
            self.UPDATE_COOKIES_JSON,
        )

        # --- assertion on interrupted OTA update --- #
        self.ota_lock.acquire.assert_called_once_with(blocking=False)
        self.ota_updater.execute.assert_called_once()
        self.ota_lock.release.assert_called_once()

        assert (
            self.ota_client.live_ota_status.get_ota_status()
            == api_types.StatusOta.FAILURE
        )
        assert self.ota_client.last_failure_type == api_types.FailureType.RECOVERABLE

    def test_rollback(self):
        # TODO
        pass

    def test_status_not_in_update(self):
        # --- query status --- #
        _status = self.ota_client.status()

        # assert v2 to v1 conversion
        assert _status.convert_to_v1() == api_types.StatusResponseEcu(
            ecu_id=self.MY_ECU_ID,
            result=api_types.FailureType.NO_FAILURE,
            status=api_types.Status(
                version=self.CURRENT_FIRMWARE_VERSION,
                status=api_types.StatusOta.SUCCESS,
            ),
        )
        # assert to v2
        assert _status == api_types.StatusResponseEcuV2(
            ecu_id=self.MY_ECU_ID,
            otaclient_version=self.OTACLIENT_VERSION,
            firmware_version=self.CURRENT_FIRMWARE_VERSION,
            failure_type=api_types.FailureType.NO_FAILURE,
            ota_status=api_types.StatusOta.SUCCESS,
        )

    def test_status_in_update(self):
        # --- mock setup --- #
        # inject ota_updater and set ota_status to UPDATING to simulate ota updating
        self.ota_client._update_executor = self.ota_updater
        self.ota_client.live_ota_status.set_ota_status(api_types.StatusOta.UPDATING)
        # let mocked updater return mocked_status_progress
        self.ota_updater.get_update_status.return_value = self.MOCKED_STATUS_PROGRESS

        # --- assertion --- #
        _status = self.ota_client.status()
        # test v2 to v1 conversion
        assert _status.convert_to_v1() == api_types.StatusResponseEcu(
            ecu_id=self.MY_ECU_ID,
            result=api_types.FailureType.NO_FAILURE,
            status=api_types.Status(
                version=self.CURRENT_FIRMWARE_VERSION,
                status=api_types.StatusOta.UPDATING,
                progress=self.MOCKED_STATUS_PROGRESS_V1,
            ),
        )
        # assert to v2
        assert _status == api_types.StatusResponseEcuV2(
            ecu_id=self.MY_ECU_ID,
            otaclient_version=self.OTACLIENT_VERSION,
            failure_type=api_types.FailureType.NO_FAILURE,
            ota_status=api_types.StatusOta.UPDATING,
            firmware_version=self.CURRENT_FIRMWARE_VERSION,
            update_status=self.MOCKED_STATUS_PROGRESS,
        )


class TestOTAServicer:
    ECU_INFO = ECUInfo(
        format_version=1,
        ecu_id="my_ecu_id",
        bootloader=BootloaderType.GRUB,
        available_ecu_ids=["my_ecu_id"],
        secondaries=[],
    )

    @pytest.fixture(autouse=True)
    async def mock_setup(self, mocker: pytest_mock.MockerFixture):
        self._executor = ThreadPoolExecutor()
        self.otaclient = mocker.MagicMock(spec=OTAClient)
        self.otaclient_cls = mocker.MagicMock(return_value=self.otaclient)
        self.standby_slot_creator_cls = mocker.MagicMock()
        self.boot_controller = mocker.MagicMock(spec=BootControllerProtocol)
        self.control_flags = mocker.MagicMock(spec=OTAClientControlFlags)

        #
        # ------ patching ------
        #
        mocker.patch(f"{cfg.OTACLIENT_MODULE_PATH}.OTAClient", self.otaclient_cls)
        mocker.patch(
            f"{cfg.OTACLIENT_MODULE_PATH}.get_boot_controller",
            return_value=mocker.MagicMock(return_value=self.boot_controller),
        )
        mocker.patch(
            f"{cfg.OTACLIENT_MODULE_PATH}.get_standby_slot_creator",
            return_value=self.standby_slot_creator_cls,
        )
        mocker.patch(f"{cfg.OTACLIENT_MODULE_PATH}.ecu_info", self.ECU_INFO)

        #
        # ------ start OTAServicer instance ------
        #
        self.local_use_proxy = ""
        self.otaclient_stub = OTAServicer(
            executor=self._executor,
            control_flags=self.control_flags,
            otaclient_version="otaclient test version",
            proxy=self.local_use_proxy,
        )

        try:
            yield
        finally:
            self._executor.shutdown(wait=False)

    def test_stub_initializing(self):
        #
        # ------ assertion ------
        #

        # ensure the OTAServicer properly compose otaclient core
        self.otaclient_cls.assert_called_once_with(
            boot_controller=self.boot_controller,
            create_standby_cls=self.standby_slot_creator_cls,
            my_ecu_id=self.ECU_INFO.ecu_id,
            control_flags=self.control_flags,
            proxy=self.local_use_proxy,
        )

        assert self.otaclient_stub.last_operation is None
        assert self.otaclient_stub.local_used_proxy_url is self.local_use_proxy

    async def test_dispatch_update(self):
        update_request_ecu = api_types.UpdateRequestEcu(
            ecu_id=self.ECU_INFO.ecu_id,
            version="version",
            url="url",
            cookies="cookies",
        )
        # patch otaclient's update method
        _updating_event = threading.Event()

        def _updating(*args, **kwargs):
            """Simulating update progress."""
            _updating_event.wait()

        self.otaclient.update.side_effect = _updating

        # dispatch update
        await self.otaclient_stub.dispatch_update(update_request_ecu)
        await asyncio.sleep(0.1)  # wait for inner async closure to run

        assert self.otaclient_stub.last_operation is api_types.StatusOta.UPDATING
        assert self.otaclient_stub.is_busy
        # test ota update/rollback exclusive lock,
        resp = await self.otaclient_stub.dispatch_update(update_request_ecu)
        assert resp.result == api_types.FailureType.RECOVERABLE

        # finish up update
        _updating_event.set()
        await asyncio.sleep(0.1)  # wait for update task return
        assert self.otaclient_stub.last_operation is None
        self.otaclient.update.assert_called_once_with(
            update_request_ecu.version,
            update_request_ecu.url,
            update_request_ecu.cookies,
        )

    async def test_dispatch_rollback(self):
        # patch otaclient's rollback method
        _rollbacking_event = threading.Event()

        def _rollbacking(*args, **kwargs):
            """Simulating rollback progress."""
            _rollbacking_event.wait()

        self.otaclient.rollback.side_effect = _rollbacking

        # dispatch rollback
        await self.otaclient_stub.dispatch_rollback(api_types.RollbackRequestEcu())
        await asyncio.sleep(0.1)  # wait for inner async closure to run

        assert self.otaclient_stub.last_operation is api_types.StatusOta.ROLLBACKING
        assert self.otaclient_stub.is_busy
        # test ota update/rollback exclusive lock,
        resp = await self.otaclient_stub.dispatch_rollback(
            api_types.RollbackRequestEcu()
        )
        assert resp.result == api_types.FailureType.RECOVERABLE

        # finish up rollback
        _rollbacking_event.set()
        await asyncio.sleep(0.1)  # wait for rollback task return
        assert self.otaclient_stub.last_operation is None
        self.otaclient.rollback.assert_called_once()

    async def test_get_status(self):
        await self.otaclient_stub.get_status()
        self.otaclient.status.assert_called_once()
