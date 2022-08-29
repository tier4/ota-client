import threading
import typing
import pytest
import pytest_mock
from pathlib import Path
from unittest.mock import ANY

from app.boot_control import BootControllerProtocol
from app.create_standby import StandbySlotCreatorProtocol
from app.errors import OTAErrorRecoverable, OTAUpdateError
from app.proto import wrapper

from tests.conftest import TestConfiguration as cfg


class Test_OTAUpdater:
    @pytest.fixture(autouse=True)
    def mock_setup(self, mocker: pytest_mock.MockerFixture):
        from app.proxy_info import ProxyInfo

        ###### mock boot_controller ######
        self._boot_control = typing.cast(
            BootControllerProtocol, mocker.MagicMock(spec=BootControllerProtocol)
        )
        self._create_standby = typing.cast(
            StandbySlotCreatorProtocol,
            mocker.MagicMock(spec=StandbySlotCreatorProtocol),
        )

        ###### mock proxy info ######
        _proxy_cfg = typing.cast(ProxyInfo, mocker.MagicMock(spec=ProxyInfo))
        _proxy_cfg.enable_local_ota_proxy = False
        _proxy_cfg.get_proxy_for_local_ota.return_value = None

        ###### patch ######
        mocker.patch(f"{cfg.OTACLIENT_MODULE_PATH}.proxy_cfg", _proxy_cfg)
        mocker.patch(f"{cfg.OTACLIENT_MODULE_PATH}.UpdateMeta", mocker.MagicMock())
        mocker.patch(
            f"{cfg.OTACLIENT_MODULE_PATH}.OTAUpdateStatsCollector", mocker.MagicMock()
        )

    def test_OTAUpdater(self, mocker: pytest_mock.MockerFixture):
        from app.ota_client import _OTAUpdater, OTAUpdateFSM

        _updater = _OTAUpdater(
            self._boot_control,
            create_standby_cls=mocker.MagicMock(return_value=self._create_standby),
        )

        _ota_update_fsm = typing.cast(OTAUpdateFSM, mocker.MagicMock(spec=OTAUpdateFSM))
        _updater.execute(
            version=cfg.UPDATE_VERSION,
            raw_url_base=cfg.OTA_IMAGE_URL,
            cookies_json=r'{"test": "my-cookie"}',
            fsm=_ota_update_fsm,
        )

        _ota_update_fsm.client_finish_update.assert_called_once()
        _ota_update_fsm.client_wait_for_reboot.assert_called_once()
        # pre_update
        assert _updater.updating_version == cfg.UPDATE_VERSION
        self._boot_control.pre_update.assert_called_once()
        # in_update
        self._create_standby.create_standby_slot.assert_called_once()
        # post_update
        self._boot_control.post_update.assert_called_once()


class Test_OTAClient:
    MOCKED_STATUS_PROGRESS = wrapper.StatusProgress(
        files_processed_copy=256,
        file_size_processed_download=128,
        file_size_processed_link=6,
    )
    UPDATE_COOKIES_JSON = r'{"test": "my-cookie"}'
    MY_ECU_ID = "autoware"

    @pytest.fixture(autouse=True)
    def mock_setup(self, tmp_path: Path, mocker: pytest_mock.MockerFixture):
        from app.ota_client import _OTAUpdater, OTAUpdateFSM

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
            f"{cfg.OTACLIENT_MODULE_PATH}.Lock", return_value=self._otaclient_lock
        )

    def test_update_normal_finished(self, mocker: pytest_mock.MockerFixture):
        from app.ota_client import OTAClient

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
        from app.ota_client import OTAClient

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
        assert _ota_client.last_failure is _error
        self._fsm.on_otaclient_failed.assert_called_once()

    def test_rollback(self):
        # TODO
        pass

    def test_status_not_in_update(self, mocker: pytest_mock.MockerFixture):
        from app.ota_client import OTAClient

        _ota_client = OTAClient(
            boot_control_cls=mocker.MagicMock(return_value=self._boot_control_mock),
            create_standby_cls=mocker.MagicMock(retun_value=self._create_standby_mock),
            my_ecu_id=self.MY_ECU_ID,
        )

        _status = _ota_client.status()
        assert _status == wrapper.StatusResponseEcu(
            ecu_id=self.MY_ECU_ID,
            result=wrapper.FailureType.NO_FAILURE.value,
            status=wrapper.v2.Status(
                version=cfg.CURRENT_VERSION,
                status=wrapper.StatusOta.SUCCESS.value,
            ),
        )

    def test_status_in_update(self, mocker: pytest_mock.MockerFixture):
        from app.ota_client import OTAClient

        _ota_client = OTAClient(
            boot_control_cls=mocker.MagicMock(return_value=self._boot_control_mock),
            create_standby_cls=mocker.MagicMock(retun_value=self._create_standby_mock),
            my_ecu_id=self.MY_ECU_ID,
        )

        ### set the ota_status to updating
        _ota_client.live_ota_status.set_ota_status(wrapper.StatusOta.UPDATING)

        _status = _ota_client.status()
        assert _status == wrapper.StatusResponseEcu(
            ecu_id=self.MY_ECU_ID,
            result=wrapper.FailureType.NO_FAILURE.value,
            status=wrapper.v2.Status(
                version=cfg.CURRENT_VERSION,
                status=wrapper.StatusOta.UPDATING.value,
                progress=self.MOCKED_STATUS_PROGRESS.unwrap(),  # type: ignore
            ),
        )
