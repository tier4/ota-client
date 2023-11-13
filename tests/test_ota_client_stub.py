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


import asyncio
import pytest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Set

import pytest_mock

from otaclient.app.ecu_info import ECUInfo
from otaclient.app.ota_client import OTAServicer
from otaclient.app.ota_client_call import OtaClientCall
from otaclient.app.ota_client_stub import (
    ECUStatusStorage,
    OTAClientServiceStub,
    OTAProxyLauncher,
)
from otaclient.app.proto import wrapper
from otaclient.app.proxy_info import ProxyInfo
from otaclient.ota_proxy import OTAProxyContextProto
from otaclient.ota_proxy.config import Config as otaproxyConfig

from tests.utils import compare_message
from tests.conftest import test_cfg

import logging

logger = logging.getLogger(__name__)


ECU_INFO_YAML = """\
format_vesrion: 1
ecu_id: "autoware"
ip_addr: "10.0.0.1"
bootloader: "grub"
secondaries:
    - ecu_id: "p1"
      ip_addr: "10.0.0.11"
    - ecu_id: "p2"
      ip_addr: "10.0.0.12"
available_ecu_ids:
    - "autoware"
    # p1: new otaclient
    - "p1"
    # p2: old otaclient
    - "p2"
"""


class _DummyOTAProxyContext(OTAProxyContextProto):
    def __init__(self, sentinel) -> None:
        self.sentinel = sentinel

    @property
    def extra_kwargs(self) -> Dict[str, Any]:
        return {}

    def __enter__(self):
        logger.info(f"touch {self.sentinel=}")
        Path(self.sentinel).touch()
        return self

    def __exit__(self, __exc_type, __exc_value, __traceback):
        return


class TestOTAProxyLauncher:
    @pytest.fixture(autouse=True)
    async def mock_setup(self, tmp_path: Path):
        proxy_info = ProxyInfo(
            gateway=False,
            upper_ota_proxy="",
            enable_local_ota_proxy=True,
            local_ota_proxy_listen_addr="127.0.0.1",
            local_ota_proxy_listen_port=8082,
        )
        proxy_server_cfg = otaproxyConfig()

        cache_base_dir = tmp_path / "ota_cache"
        self.sentinel_file = tmp_path / "otaproxy_sentinel"
        proxy_server_cfg.BASE_DIR = str(cache_base_dir)  # type: ignore
        proxy_server_cfg.DB_FILE = str(cache_base_dir / "cache_db")  # type: ignore

        # init launcher inst
        threadpool = ThreadPoolExecutor()
        self.otaproxy_launcher = OTAProxyLauncher(
            executor=threadpool,
            subprocess_ctx=_DummyOTAProxyContext(str(self.sentinel_file)),
            _proxy_info=proxy_info,
            _proxy_server_cfg=proxy_server_cfg,
        )

        try:
            yield
        finally:
            threadpool.shutdown()

    async def test_start_stop(self):
        # startup
        # --- execution --- #
        _pid = await self.otaproxy_launcher.start(init_cache=True)
        await asyncio.sleep(3)  # wait for subprocess_init finish execution

        # --- assertion --- #
        assert self.otaproxy_launcher.is_running
        assert self.sentinel_file.is_file()
        assert _pid is not None and _pid > 0

        # shutdown
        # --- execution --- #
        # NOTE: save the subprocess ref as stop method will de-refer it
        _old_subprocess = self.otaproxy_launcher._otaproxy_subprocess
        await self.otaproxy_launcher.stop()

        # --- assertion --- #
        assert not self.otaproxy_launcher.is_running
        assert self.otaproxy_launcher._otaproxy_subprocess is None
        assert _old_subprocess and not _old_subprocess.is_alive()


class TestECUStatusStorage:
    PROPERTY_REFRESH_INTERVAL_FOR_TEST = 1
    SAFE_INTERVAL_FOR_PROPERTY_UPDATE = 1.2

    @pytest.fixture(autouse=True)
    async def setup_test(self, tmp_path: Path):
        ecu_info_f = tmp_path / "ecu_info.yml"
        ecu_info_f.write_text(ECU_INFO_YAML)
        self.ecu_info = ECUInfo.parse_ecu_info(ecu_info_f)

        # init and setup the ecu_storage
        self.ecu_storage = ECUStatusStorage(self.ecu_info)
        # NOTE: decrease the interval for faster testing
        self.ecu_storage.PROPERTY_REFRESH_INTERVAL = self.PROPERTY_REFRESH_INTERVAL_FOR_TEST  # type: ignore

        try:
            yield
        finally:
            self.ecu_storage._debug_properties_update_shutdown_event.set()
            await asyncio.sleep(self.SAFE_INTERVAL_FOR_PROPERTY_UPDATE)

    @pytest.mark.parametrize(
        "local_ecu_status,sub_ecus_status,expected",
        (
            # case 1
            (
                # local ECU's status report
                wrapper.StatusResponseEcuV2(
                    ecu_id="autoware",
                    ota_status=wrapper.StatusOta.SUCCESS,
                    firmware_version="123.x",
                    failure_type=wrapper.FailureType.NO_FAILURE,
                ),
                # sub ECU's status report
                [
                    wrapper.StatusResponse(
                        available_ecu_ids=["p1"],
                        ecu_v2=[
                            wrapper.StatusResponseEcuV2(
                                ecu_id="p1",
                                ota_status=wrapper.StatusOta.SUCCESS,
                                firmware_version="123.x",
                                failure_type=wrapper.FailureType.NO_FAILURE,
                            )
                        ],
                    ),
                    wrapper.StatusResponse(
                        available_ecu_ids=["p2"],
                        ecu=[
                            wrapper.StatusResponseEcu(
                                ecu_id="p2",
                                result=wrapper.FailureType.NO_FAILURE,
                                status=wrapper.Status(
                                    status=wrapper.StatusOta.SUCCESS,
                                    version="123.x",
                                ),
                            ),
                        ],
                    ),
                ],
                # expected export
                wrapper.StatusResponse(
                    available_ecu_ids=["autoware", "p1", "p2"],
                    # explicitly v1 format compatibility
                    ecu=[
                        wrapper.StatusResponseEcu(
                            ecu_id="autoware",
                            result=wrapper.FailureType.NO_FAILURE,
                            status=wrapper.Status(
                                status=wrapper.StatusOta.SUCCESS,
                                version="123.x",
                            ),
                        ),
                        wrapper.StatusResponseEcu(
                            ecu_id="p1",
                            result=wrapper.FailureType.NO_FAILURE,
                            status=wrapper.Status(
                                status=wrapper.StatusOta.SUCCESS,
                                version="123.x",
                            ),
                        ),
                        wrapper.StatusResponseEcu(
                            ecu_id="p2",
                            result=wrapper.FailureType.NO_FAILURE,
                            status=wrapper.Status(
                                status=wrapper.StatusOta.SUCCESS,
                                version="123.x",
                            ),
                        ),
                    ],
                    ecu_v2=[
                        wrapper.StatusResponseEcuV2(
                            ecu_id="autoware",
                            ota_status=wrapper.StatusOta.SUCCESS,
                            failure_type=wrapper.FailureType.NO_FAILURE,
                            firmware_version="123.x",
                        ),
                        wrapper.StatusResponseEcuV2(
                            ecu_id="p1",
                            ota_status=wrapper.StatusOta.SUCCESS,
                            failure_type=wrapper.FailureType.NO_FAILURE,
                            firmware_version="123.x",
                        ),
                    ],
                ),
            ),  # case 1
            # case 2
            (
                # local ecu status report
                wrapper.StatusResponseEcuV2(
                    ecu_id="autoware",
                    ota_status=wrapper.StatusOta.UPDATING,
                    firmware_version="123.x",
                    failure_type=wrapper.FailureType.NO_FAILURE,
                    update_status=wrapper.UpdateStatus(
                        update_firmware_version="789.x",
                        phase=wrapper.UpdatePhase.DOWNLOADING_OTA_FILES,
                        total_elapsed_time=wrapper.Duration(seconds=123),
                        total_files_num=123456,
                        processed_files_num=123,
                        processed_files_size=456,
                        downloaded_bytes=789,
                        downloaded_files_num=100,
                        downloaded_files_size=400,
                    ),
                ),
                # sub ECUs' status report
                [
                    wrapper.StatusResponse(
                        available_ecu_ids=["p1"],
                        ecu_v2=[
                            wrapper.StatusResponseEcuV2(
                                ecu_id="p1",
                                ota_status=wrapper.StatusOta.UPDATING,
                                firmware_version="123.x",
                                failure_type=wrapper.FailureType.NO_FAILURE,
                                update_status=wrapper.UpdateStatus(
                                    update_firmware_version="789.x",
                                    phase=wrapper.UpdatePhase.DOWNLOADING_OTA_FILES,
                                    total_elapsed_time=wrapper.Duration(seconds=123),
                                    total_files_num=123456,
                                    processed_files_num=123,
                                    processed_files_size=456,
                                    downloaded_bytes=789,
                                    downloaded_files_num=100,
                                    downloaded_files_size=400,
                                ),
                            )
                        ],
                    ),
                    wrapper.StatusResponse(
                        available_ecu_ids=["p2"],
                        ecu=[
                            wrapper.StatusResponseEcu(
                                ecu_id="p2",
                                result=wrapper.FailureType.NO_FAILURE,
                                status=wrapper.Status(
                                    status=wrapper.StatusOta.SUCCESS,
                                    version="123.x",
                                ),
                            ),
                        ],
                    ),
                ],
                # expected export result
                wrapper.StatusResponse(
                    available_ecu_ids=["autoware", "p1", "p2"],
                    # explicitly v1 format compatibility
                    # NOTE: processed_files_num(v2) = files_processed_download(v1) + files_processed_copy(v1)
                    # check wrapper.UpdateStatus.convert_to_v1_StatusProgress for more details.
                    ecu=[
                        wrapper.StatusResponseEcu(
                            ecu_id="autoware",
                            result=wrapper.FailureType.NO_FAILURE,
                            status=wrapper.Status(
                                status=wrapper.StatusOta.UPDATING,
                                version="123.x",
                                progress=wrapper.StatusProgress(
                                    phase=wrapper.StatusProgressPhase.REGULAR,
                                    total_regular_files=123456,
                                    files_processed_download=100,
                                    file_size_processed_download=400,
                                    files_processed_copy=23,
                                    file_size_processed_copy=56,
                                    download_bytes=789,
                                    regular_files_processed=123,
                                    total_elapsed_time=wrapper.Duration(seconds=123),
                                ),
                            ),
                        ),
                        wrapper.StatusResponseEcu(
                            ecu_id="p1",
                            result=wrapper.FailureType.NO_FAILURE,
                            status=wrapper.Status(
                                status=wrapper.StatusOta.UPDATING,
                                version="123.x",
                                progress=wrapper.StatusProgress(
                                    phase=wrapper.StatusProgressPhase.REGULAR,
                                    total_regular_files=123456,
                                    files_processed_download=100,
                                    file_size_processed_download=400,
                                    files_processed_copy=23,
                                    file_size_processed_copy=56,
                                    download_bytes=789,
                                    regular_files_processed=123,
                                    total_elapsed_time=wrapper.Duration(seconds=123),
                                ),
                            ),
                        ),
                        wrapper.StatusResponseEcu(
                            ecu_id="p2",
                            result=wrapper.FailureType.NO_FAILURE,
                            status=wrapper.Status(
                                version="123.x",
                                status=wrapper.StatusOta.SUCCESS,
                            ),
                        ),
                    ],
                    ecu_v2=[
                        wrapper.StatusResponseEcuV2(
                            ecu_id="autoware",
                            ota_status=wrapper.StatusOta.UPDATING,
                            failure_type=wrapper.FailureType.NO_FAILURE,
                            firmware_version="123.x",
                            update_status=wrapper.UpdateStatus(
                                update_firmware_version="789.x",
                                phase=wrapper.UpdatePhase.DOWNLOADING_OTA_FILES,
                                total_elapsed_time=wrapper.Duration(seconds=123),
                                total_files_num=123456,
                                processed_files_num=123,
                                processed_files_size=456,
                                downloaded_bytes=789,
                                downloaded_files_num=100,
                                downloaded_files_size=400,
                            ),
                        ),
                        wrapper.StatusResponseEcuV2(
                            ecu_id="p1",
                            ota_status=wrapper.StatusOta.UPDATING,
                            failure_type=wrapper.FailureType.NO_FAILURE,
                            firmware_version="123.x",
                            update_status=wrapper.UpdateStatus(
                                update_firmware_version="789.x",
                                phase=wrapper.UpdatePhase.DOWNLOADING_OTA_FILES,
                                total_elapsed_time=wrapper.Duration(seconds=123),
                                total_files_num=123456,
                                processed_files_num=123,
                                processed_files_size=456,
                                downloaded_bytes=789,
                                downloaded_files_num=100,
                                downloaded_files_size=400,
                            ),
                        ),
                    ],
                ),
            ),  # case 2
        ),
    )
    async def test_export(
        self,
        local_ecu_status: wrapper.StatusResponseEcuV2,
        sub_ecus_status: List[wrapper.StatusResponse],
        expected: wrapper.StatusResponse,
    ):
        # --- prepare --- #
        await self.ecu_storage.update_from_local_ecu(local_ecu_status)
        for ecu_status_report in sub_ecus_status:
            await self.ecu_storage.update_from_child_ecu(ecu_status_report)

        # --- execution --- #
        exported = await self.ecu_storage.export()

        # ---  assertion --- #
        compare_message(exported, expected)

    @pytest.mark.parametrize(
        "local_ecu_status,sub_ecus_status,properties_dict",
        (
            # case 1:
            (
                # local ECU status: UPDATING, requires network
                wrapper.StatusResponseEcuV2(
                    ecu_id="autoware",
                    ota_status=wrapper.StatusOta.UPDATING,
                    update_status=wrapper.UpdateStatus(
                        phase=wrapper.UpdatePhase.DOWNLOADING_OTA_FILES
                    ),
                ),
                # sub ECUs status
                [
                    wrapper.StatusResponse(
                        available_ecu_ids=["p1"],
                        ecu_v2=[
                            wrapper.StatusResponseEcuV2(
                                ecu_id="p1",
                                ota_status=wrapper.StatusOta.FAILURE,
                            ),
                        ],
                    ),
                    # p2: updating, doesn't require network
                    wrapper.StatusResponse(
                        available_ecu_ids=["p2"],
                        ecu=[
                            wrapper.StatusResponseEcu(
                                ecu_id="p2",
                                status=wrapper.Status(
                                    status=wrapper.StatusOta.UPDATING,
                                    progress=wrapper.StatusProgress(
                                        phase=wrapper.StatusProgressPhase.POST_PROCESSING,
                                    ),
                                ),
                            )
                        ],
                    ),
                ],
                # expected overal ECUs status report
                {
                    "lost_ecus_id": set(),
                    "in_update_ecus_id": {"autoware", "p2"},
                    "in_update_child_ecus_id": {"p2"},
                    "failed_ecus_id": {"p1"},
                    "any_requires_network": True,
                    "success_ecus_id": set(),
                    "all_success": False,
                },
            ),
            # case 2:
            (
                # local ECU status: SUCCESS
                wrapper.StatusResponseEcuV2(
                    ecu_id="autoware",
                    ota_status=wrapper.StatusOta.SUCCESS,
                ),
                # sub ECUs status
                [
                    # p1: FAILURE
                    wrapper.StatusResponse(
                        available_ecu_ids=["p1"],
                        ecu_v2=[
                            wrapper.StatusResponseEcuV2(
                                ecu_id="p1",
                                ota_status=wrapper.StatusOta.FAILURE,
                            ),
                        ],
                    ),
                    # p2: updating, requires network
                    wrapper.StatusResponse(
                        available_ecu_ids=["p2"],
                        ecu=[
                            wrapper.StatusResponseEcu(
                                ecu_id="p2",
                                status=wrapper.Status(
                                    status=wrapper.StatusOta.UPDATING,
                                    progress=wrapper.StatusProgress(
                                        phase=wrapper.StatusProgressPhase.REGULAR,
                                    ),
                                ),
                            )
                        ],
                    ),
                ],
                # expected overal ECUs status report
                {
                    "lost_ecus_id": set(),
                    "in_update_ecus_id": {"p2"},
                    "in_update_child_ecus_id": {"p2"},
                    "failed_ecus_id": {"p1"},
                    "any_requires_network": True,
                    "success_ecus_id": {"autoware"},
                    "all_success": False,
                },
            ),
        ),
    )
    async def test_overall_ecu_status_report_generation(
        self,
        local_ecu_status: wrapper.StatusResponseEcuV2,
        sub_ecus_status: List[wrapper.StatusResponse],
        properties_dict: Dict[str, Any],
    ):
        # --- prepare --- #
        await self.ecu_storage.update_from_local_ecu(local_ecu_status)
        for ecu_status_report in sub_ecus_status:
            await self.ecu_storage.update_from_child_ecu(ecu_status_report)
        await asyncio.sleep(
            self.SAFE_INTERVAL_FOR_PROPERTY_UPDATE
        )  # wait for status report generation

        # --- assertion --- #
        for k, v in properties_dict.items():
            assert getattr(self.ecu_storage, k) == v, f"status_report attr {k} mismatch"

    @pytest.mark.parametrize(
        "local_ecu_status,sub_ecus_status,ecus_accept_update_request,properties_dict",
        (
            # case 1:
            #   There is FAILED/UPDATING ECUs existed in the cluster.
            #   We try to retry OTA update only on failed autoware ECU.
            #   on_ecus_accept_update_request should only change the overall status report
            #   based on the status change of ECUs that accept update request.
            (
                # local ECU status: FAILED
                wrapper.StatusResponseEcuV2(
                    ecu_id="autoware",
                    ota_status=wrapper.StatusOta.FAILURE,
                ),
                # sub ECUs status
                [
                    # p1: FAILED
                    wrapper.StatusResponse(
                        available_ecu_ids=["p1"],
                        ecu_v2=[
                            wrapper.StatusResponseEcuV2(
                                ecu_id="p1",
                                ota_status=wrapper.StatusOta.FAILURE,
                            ),
                        ],
                    ),
                    # p2: UPDATING
                    wrapper.StatusResponse(
                        available_ecu_ids=["p2"],
                        ecu=[
                            wrapper.StatusResponseEcu(
                                ecu_id="p2",
                                status=wrapper.Status(
                                    status=wrapper.StatusOta.UPDATING,
                                    progress=wrapper.StatusProgress(
                                        phase=wrapper.StatusProgressPhase.REGULAR,
                                    ),
                                ),
                            )
                        ],
                    ),
                ],
                ["autoware"],
                # expected overal ECUs status report set by on_ecus_accept_update_request
                {
                    "lost_ecus_id": set(),
                    "in_update_ecus_id": {"autoware", "p2"},
                    "in_update_child_ecus_id": {"p2"},
                    "failed_ecus_id": {"p1"},
                    "any_requires_network": True,
                    "success_ecus_id": set(),
                    "all_success": False,
                },
            ),
            # case 2:
            #   There is FAILED/UPDATING/SUCCESS ECUs existed in the cluster.
            #   We try to retry OTA update only on failed autoware ECU.
            #   on_ecus_accept_update_request should only change the overall status report
            #   based on the status change of ECUs that accept update request.
            (
                # local ECU status: UPDATING
                wrapper.StatusResponseEcuV2(
                    ecu_id="autoware",
                    ota_status=wrapper.StatusOta.UPDATING,
                    update_status=wrapper.UpdateStatus(
                        phase=wrapper.UpdatePhase.DOWNLOADING_OTA_FILES,
                    ),
                ),
                # sub ECUs status
                [
                    # p1: FAILED
                    wrapper.StatusResponse(
                        available_ecu_ids=["p1"],
                        ecu_v2=[
                            wrapper.StatusResponseEcuV2(
                                ecu_id="p1",
                                ota_status=wrapper.StatusOta.FAILURE,
                            ),
                        ],
                    ),
                    # p2: SUCCESS
                    wrapper.StatusResponse(
                        available_ecu_ids=["p2"],
                        ecu=[
                            wrapper.StatusResponseEcu(
                                ecu_id="p2",
                                status=wrapper.Status(
                                    status=wrapper.StatusOta.SUCCESS,
                                ),
                            )
                        ],
                    ),
                ],
                ["p1"],
                # expected overal ECUs status report set by on_ecus_accept_update_request,
                {
                    "lost_ecus_id": set(),
                    "in_update_ecus_id": {"autoware", "p1"},
                    "in_update_child_ecus_id": {"p1"},
                    "failed_ecus_id": set(),
                    "any_requires_network": True,
                    "success_ecus_id": {"p2"},
                    "all_success": False,
                },
            ),
        ),
    )
    async def test_on_receive_update_request(
        self,
        local_ecu_status: wrapper.StatusResponseEcuV2,
        sub_ecus_status: List[wrapper.StatusResponse],
        ecus_accept_update_request: List[str],
        properties_dict: Dict[str, Any],
    ):
        # --- prepare --- #
        await self.ecu_storage.update_from_local_ecu(local_ecu_status)
        for ecu_status_report in sub_ecus_status:
            await self.ecu_storage.update_from_child_ecu(ecu_status_report)
        await asyncio.sleep(
            self.SAFE_INTERVAL_FOR_PROPERTY_UPDATE
        )  # wait for status report generation

        # --- execution --- #
        # NOTE: prevent overall ECU status report generation to check the
        #       values generated by on_ecus_accept_update_request.
        self.ecu_storage.DELAY_OVERALL_STATUS_REPORT_UPDATE = 999  # type: ignore
        await self.ecu_storage.on_ecus_accept_update_request(
            set(ecus_accept_update_request)
        )

        # --- assertion --- #
        for k, v in properties_dict.items():
            assert getattr(self.ecu_storage, k) == v, f"status_report attr {k} mismatch"
        assert self.ecu_storage.active_ota_update_present.is_set()

    async def test_polling_waiter_switching_from_idling_to_active(self):
        """Waiter should immediately return if active_ota_update_present is set."""
        _sleep_time, _mocked_interval = 3, 60
        self.ecu_storage.IDLE_POLLING_INTERVAL = _mocked_interval  # type: ignore

        async def _event_setter():
            await asyncio.sleep(_sleep_time)
            self.ecu_storage.active_ota_update_present.set()

        self.ecu_storage.active_ota_update_present.clear()
        _waiter = self.ecu_storage.get_polling_waiter()
        asyncio.create_task(_event_setter())
        # waiter should return on active_ota_update_present is set, instead of waiting the
        #   full <IDLE_POLLING_INTERVAL>.
        # expected behavior:
        #   1. wait until _event_setter finished, or with a little bit delay.
        #   2. wait much less time than <_mocked_interval>.
        await asyncio.wait_for(_waiter(), timeout=_sleep_time + 1)


class TestOTAClientServiceStub:
    POLLING_INTERVAL = 1
    ENSURE_NEXT_CHECKING_ROUND = 1.2

    @staticmethod
    async def _subecu_accept_update_request(ecu_id, *args, **kwargs):
        return wrapper.UpdateResponse(
            ecu=[
                wrapper.UpdateResponseEcu(
                    ecu_id=ecu_id, result=wrapper.FailureType.NO_FAILURE
                )
            ]
        )

    @pytest.fixture(autouse=True)
    async def setup_test(self, tmp_path: Path, mocker: pytest_mock.MockerFixture):
        threadpool = ThreadPoolExecutor()

        # prepare ecu_info
        ecu_info_f = tmp_path / "ecu_info.yml"
        ecu_info_f.write_text(ECU_INFO_YAML)
        self.ecu_info = ECUInfo.parse_ecu_info(ecu_info_f)

        # init and setup the ecu_storage
        self.ecu_storage = ECUStatusStorage(self.ecu_info)
        self.ecu_storage.on_ecus_accept_update_request = mocker.AsyncMock()
        # NOTE: decrease the interval to speed up testing
        #       (used by _otaproxy_lifecycle_managing/_otaclient_control_flags_managing task)
        self.ecu_storage.ACTIVE_POLLING_INTERVAL = self.POLLING_INTERVAL  # type: ignore
        self.ecu_storage.IDLE_POLLING_INTERVAL = self.POLLING_INTERVAL  # type: ignore
        # NOTE: disable internal overall ecu status generation task as we
        #       will manipulate the values by ourselves.
        self.ecu_storage._debug_properties_update_shutdown_event.set()
        await asyncio.sleep(self.ENSURE_NEXT_CHECKING_ROUND)  # ensure the task stopping

        # --- mocker --- #
        self.otaclient_wrapper = mocker.MagicMock(spec=OTAServicer)
        self.ecu_status_tracker = mocker.MagicMock()
        self.otaproxy_launcher = mocker.MagicMock(spec=OTAProxyLauncher)
        # mock OTAClientCall, make update_call return success on any update dispatches to subECUs
        self.otaclient_call = mocker.AsyncMock(spec=OtaClientCall)
        self.otaclient_call.update_call = mocker.AsyncMock(
            wraps=self._subecu_accept_update_request
        )
        # proxy_info
        self.proxy_info = ProxyInfo(
            enable_local_ota_proxy=True,
            upper_ota_proxy="",
        )

        # --- patching and mocking --- #
        mocker.patch(
            f"{test_cfg.OTACLIENT_STUB_MODULE_PATH}.ECUStatusStorage",
            mocker.MagicMock(return_value=self.ecu_storage),
        )
        mocker.patch(
            f"{test_cfg.OTACLIENT_STUB_MODULE_PATH}.OTAServicer",
            mocker.MagicMock(return_value=self.otaclient_wrapper),
        )
        mocker.patch(
            f"{test_cfg.OTACLIENT_STUB_MODULE_PATH}._ECUTracker",
            mocker.MagicMock(return_value=self.ecu_status_tracker),
        )
        mocker.patch(
            f"{test_cfg.OTACLIENT_STUB_MODULE_PATH}.OTAProxyLauncher",
            mocker.MagicMock(return_value=self.otaproxy_launcher),
        )
        mocker.patch(
            f"{test_cfg.OTACLIENT_STUB_MODULE_PATH}.OtaClientCall", self.otaclient_call
        )

        # --- start the OTAClientServiceStub --- #
        self.otaclient_service_stub = OTAClientServiceStub(
            ecu_info=self.ecu_info, _proxy_cfg=self.proxy_info
        )

        try:
            yield
        finally:
            self.otaclient_service_stub._debug_status_checking_shutdown_event.set()
            threadpool.shutdown()
            await asyncio.sleep(self.ENSURE_NEXT_CHECKING_ROUND)  # ensure shutdown

    async def test__otaproxy_lifecycle_managing(self):
        """
        otaproxy startup/shutdown is only controlled by any_requires_network
        in overall ECU status report.
        """
        # ------ otaproxy startup ------- #
        # --- prepartion --- #
        self.otaproxy_launcher.is_running = False
        self.ecu_storage.any_requires_network = True

        # --- wait for execution --- #
        # wait for _otaproxy_lifecycle_managing to launch
        # the otaproxy on overall ecu status changed
        await asyncio.sleep(self.ENSURE_NEXT_CHECKING_ROUND)

        # --- assertion --- #
        self.otaproxy_launcher.start.assert_called_once()

        # ------ otaproxy shutdown ------ #
        # --- prepartion --- #
        # set the OTAPROXY_SHUTDOWN_DELAY to allow start/stop in single test
        self.otaclient_service_stub.OTAPROXY_SHUTDOWN_DELAY = 1  # type: ignore
        self.otaproxy_launcher.is_running = True
        self.ecu_storage.any_requires_network = False

        # --- wait for execution --- #
        # wait for _otaproxy_lifecycle_managing to shutdown
        # the otaproxy on overall ecu status changed
        await asyncio.sleep(self.ENSURE_NEXT_CHECKING_ROUND)

        # --- assertion --- #
        self.otaproxy_launcher.stop.assert_called_once()

        # ---- cache dir cleanup --- #
        # only cleanup cache dir on all ECUs in SUCCESS ota_status
        self.ecu_storage.any_requires_network = False
        self.ecu_storage.all_success = True
        self.otaproxy_launcher.is_running = False
        await asyncio.sleep(self.ENSURE_NEXT_CHECKING_ROUND)

        # --- assertion --- #
        self.otaproxy_launcher.cleanup_cache_dir.assert_called_once()

    async def test__otaclient_control_flags_managing(self):
        otaclient_control_flags = self.otaclient_service_stub._otaclient_control_flags
        # there are child ECUs in UPDATING
        self.ecu_storage.in_update_child_ecus_id = {"p1", "p2"}
        await asyncio.sleep(self.ENSURE_NEXT_CHECKING_ROUND)
        assert not otaclient_control_flags._can_reboot.is_set()

        # no more child ECUs in UPDATING
        self.ecu_storage.in_update_child_ecus_id = set()
        await asyncio.sleep(self.ENSURE_NEXT_CHECKING_ROUND)
        assert otaclient_control_flags._can_reboot.is_set()

    @pytest.mark.parametrize(
        "update_request, update_target_ids, expected",
        (
            # update request for autoware, p1 ecus
            (
                wrapper.UpdateRequest(
                    ecu=[
                        wrapper.UpdateRequestEcu(
                            ecu_id="autoware",
                            version="789.x",
                            url="url",
                            cookies="cookies",
                        ),
                        wrapper.UpdateRequestEcu(
                            ecu_id="p1",
                            version="789.x",
                            url="url",
                            cookies="cookies",
                        ),
                    ]
                ),
                {"autoware", "p1"},
                # NOTE: order matters!
                #       update request dispatching to subECUs happens first,
                #       and then to the local ECU.
                wrapper.UpdateResponse(
                    ecu=[
                        wrapper.UpdateResponseEcu(
                            ecu_id="p1",
                            result=wrapper.FailureType.NO_FAILURE,
                        ),
                        wrapper.UpdateResponseEcu(
                            ecu_id="autoware",
                            result=wrapper.FailureType.NO_FAILURE,
                        ),
                    ]
                ),
            ),
            # update only p2
            (
                wrapper.UpdateRequest(
                    ecu=[
                        wrapper.UpdateRequestEcu(
                            ecu_id="p2",
                            version="789.x",
                            url="url",
                            cookies="cookies",
                        ),
                    ]
                ),
                {"p2"},
                wrapper.UpdateResponse(
                    ecu=[
                        wrapper.UpdateResponseEcu(
                            ecu_id="p2",
                            result=wrapper.FailureType.NO_FAILURE,
                        ),
                    ]
                ),
            ),
        ),
    )
    async def test_update_normal(
        self,
        update_request: wrapper.UpdateRequest,
        update_target_ids: Set[str],
        expected: wrapper.UpdateResponse,
    ):
        # --- setup --- #
        self.otaclient_wrapper.dispatch_update.return_value = wrapper.UpdateResponseEcu(
            ecu_id=self.ecu_info.ecu_id, result=wrapper.FailureType.NO_FAILURE
        )

        # --- execution --- #
        resp = await self.otaclient_service_stub.update(update_request)

        # --- assertion --- #
        compare_message(resp, expected)
        self.otaclient_call.update_call.assert_called()
        self.ecu_storage.on_ecus_accept_update_request.assert_called_once_with(
            update_target_ids
        )

    async def test_update_local_ecu_busy(self):
        # --- preparation --- #
        self.otaclient_wrapper.dispatch_update.return_value = wrapper.UpdateResponseEcu(
            ecu_id="autoware", result=wrapper.FailureType.RECOVERABLE
        )
        update_request_ecu = wrapper.UpdateRequestEcu(
            ecu_id="autoware", version="version", url="url", cookies="cookies"
        )

        # --- execution --- #
        resp = await self.otaclient_service_stub.update(
            wrapper.UpdateRequest(ecu=[update_request_ecu])
        )

        # --- assertion --- #
        assert resp == wrapper.UpdateResponse(
            ecu=[
                wrapper.UpdateResponseEcu(
                    ecu_id="autoware",
                    result=wrapper.FailureType.RECOVERABLE,
                )
            ]
        )
        self.otaclient_wrapper.dispatch_update.assert_called_once_with(
            update_request_ecu
        )
