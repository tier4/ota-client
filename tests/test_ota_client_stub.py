import asyncio
import pytest
import pytest_mock
import time
import typing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List

from tests.utils import DummySubECU
from tests.conftest import TestConfiguration as cfg


class _DummySubECUsGroup:
    def __init__(self, ecu_id_list: List[str]) -> None:
        self._ecu_dict = {ecu_id: DummySubECU(ecu_id=ecu_id) for ecu_id in ecu_id_list}

    def start_update(self, ecu_id, *args, request, **kwargs):
        self._ecu_dict[ecu_id].start()
        return request

    def get_status(self, ecu_id, *args, **kwargs):
        return self._ecu_dict[ecu_id].status()


class TestOtaProxyWrapper:
    @pytest.fixture
    def mock_cfg(self, tmp_path: Path, mocker: pytest_mock.MockerFixture):
        from app.proxy_info import ProxyInfo
        from ota_proxy.config import Config

        _proxy_cfg = ProxyInfo()
        _proxy_cfg.enable_local_ota_proxy = True
        _proxy_cfg.gateway = False  # disable HTTPS
        mocker.patch(f"{cfg.OTACLIENT_STUB_MODULE_PATH}.proxy_cfg", _proxy_cfg)

        ota_cache_dir = tmp_path / "ota-cache"
        ota_cache_dir.mkdir()
        _ota_proxy_cfg = Config()
        _ota_proxy_cfg.BASE_DIR = str(ota_cache_dir)
        mocker.patch(f"{cfg.OTAPROXY_MODULE_PATH}.ota_cache.cfg", _ota_proxy_cfg)

    @pytest.fixture
    def ota_proxy_instance(self, mocker: pytest_mock.MockerFixture, mock_cfg):
        from app.ota_client_stub import OtaProxyWrapper

        _ota_proxy_wrapper = OtaProxyWrapper()
        self._ota_proxy_instance = _ota_proxy_wrapper
        try:
            yield _ota_proxy_wrapper.start(
                init_cache=True,
                wait_on_scrub=False,
            )
        finally:
            _ota_proxy_wrapper.stop()

    def test_OtaProxyWrapper(self, ota_proxy_instance):
        # TODO: ensure that the ota_proxy is launched and functional
        #       by downloading a file with proxy
        assert self._ota_proxy_instance.is_running()
        assert (
            self._ota_proxy_instance._server_p
            and self._ota_proxy_instance._server_p.is_alive()
        )


class Test_UpdateSession:
    LOCAL_UPDATE_TIME_COST = 1
    SUBECU_UPDATE_TIME_COST = 2

    @pytest.fixture(autouse=True)
    def _setup_executor(self):
        try:
            self._executor = ThreadPoolExecutor()
            yield
        finally:
            self._executor.shutdown()

    @pytest.fixture(autouse=True)
    def mock_setup(self, mocker: pytest_mock.MockerFixture):
        from app.ota_client import OTAUpdateFSM

        _ota_update_fsm = typing.cast(OTAUpdateFSM, mocker.MagicMock(spec=OTAUpdateFSM))
        _ota_update_fsm.stub_wait_for_local_update = mocker.MagicMock(
            wraps=self._local_update_waiter
        )
        mocker.patch(
            f"{cfg.OTACLIENT_STUB_MODULE_PATH}.OTAUpdateFSM",
            return_value=_ota_update_fsm,
        )
        self._fsm = _ota_update_fsm

    def _local_update_waiter(self):
        time.sleep(self.LOCAL_UPDATE_TIME_COST)
        return True

    async def _subecu_update(self):
        await asyncio.sleep(self.SUBECU_UPDATE_TIME_COST)
        return True

    async def test_my_ecu_update_tracker(self):
        from app.ota_client_stub import _UpdateSession

        await _UpdateSession.my_ecu_update_tracker(
            fsm=self._fsm,
            executor=self._executor,
        )
        self._fsm.stub_wait_for_local_update.assert_called_once()

    async def test_update_tracker(self):
        from app.ota_client_stub import _UpdateSession

        # launch update session
        _update_session = _UpdateSession(executor=self._executor)

        ###### prepare tracking coro ######
        _my_ecu_tracking_task = _update_session.my_ecu_update_tracker(
            fsm=self._fsm,
            executor=self._executor,
        )
        _subecu_tracking_task = self._subecu_update()

        await _update_session.start(None)
        await _update_session.update_tracker(
            my_ecu_tracking_task=_my_ecu_tracking_task,
            subecu_tracking_task=_subecu_tracking_task,
        )

        ###### assert ######
        assert not _update_session.is_started()
        self._fsm.stub_wait_for_local_update.assert_called_once()
        self._fsm.stub_cleanup_finished.assert_called_once()


class Test_SubECUTracker:
    # TODO: remember to mock the QUERYING_SUBECU_STATUS_TIMEOUT to a small number
    pass


class TestOtaClientStub:
    pass
    # TODO: updater/rollback: test whether the all the subecus received update requests or not
    # TODO: status
