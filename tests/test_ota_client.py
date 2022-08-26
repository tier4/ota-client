import typing
import pytest
import pytest_mock
from app.proto import wrapper

from tests.conftest import TestConfiguration as cfg

from app.boot_control import BootControllerProtocol
from app.create_standby import StandbySlotCreatorProtocol


class Test_OTAUpdater:
    @pytest.fixture(autouse=True)
    def mock_setup(self, mocker: pytest_mock.MockerFixture):
        from app.proxy_info import ProxyInfo

        ###### mock boot_controller ######
        self._boot_control = mocker.MagicMock(spec=BootControllerProtocol)
        self._create_standby = mocker.MagicMock(spec=StandbySlotCreatorProtocol)

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

        # TODO: assert


class Test_OTAClient:
    MOCKED_STATUS_PROGRESS = wrapper.StatusProgress(
        files_processed_copy=256,
        file_size_processed_download=128,
        file_size_processed_link=6,
    )

    # TODO: check error handling framework
    @pytest.fixture(autouse=True)
    def mock_setup(self):
        pass

    def test_update(self):
        pass

    def test_rollback(self):
        pass

    def test_status(self):
        pass
