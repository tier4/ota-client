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
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Set

import pytest
from pytest_mock import MockerFixture

from ota_proxy import OTAProxyContextProto
from ota_proxy.config import Config as otaproxyConfig
from otaclient.app.ota_client import OTAServicer
from otaclient.app.ota_client_stub import (
    ECUStatusStorage,
    OTAClientServiceStub,
    OTAProxyLauncher,
)
from otaclient.configs.ecu_info import ECUInfo, parse_ecu_info
from otaclient.configs.proxy_info import ProxyInfo, parse_proxy_info
from otaclient_api.v2 import types as api_types
from otaclient_api.v2.api_caller import OTAClientCall
from tests.conftest import cfg
from tests.utils import compare_message

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

PROXY_INFO_YAML = """\
gateway_otaproxy: false,
enable_local_ota_proxy: true
local_ota_proxy_listen_addr: "127.0.0.1"
local_ota_proxy_listen_port: 8082
"""


@pytest.fixture
def ecu_info_fixture(tmp_path: Path) -> ECUInfo:
    _yaml_f = tmp_path / "ecu_info.yaml"
    _yaml_f.write_text(ECU_INFO_YAML)
    return parse_ecu_info(_yaml_f)


@pytest.fixture
def proxy_info_fixture(tmp_path: Path) -> ProxyInfo:
    _yaml_f = tmp_path / "proxy_info.yaml"
    _yaml_f.write_text(PROXY_INFO_YAML)
    return parse_proxy_info(_yaml_f)


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
    async def mock_setup(
        self, mocker: MockerFixture, tmp_path: Path, proxy_info_fixture
    ):
        cache_base_dir = tmp_path / "ota_cache"
        self.sentinel_file = tmp_path / "otaproxy_sentinel"

        # ------ prepare mocked proxy_info and otaproxy_cfg ------ #
        self.proxy_info = proxy_info = proxy_info_fixture
        self.proxy_server_cfg = proxy_server_cfg = otaproxyConfig()
        proxy_server_cfg.BASE_DIR = str(cache_base_dir)  # type: ignore
        proxy_server_cfg.DB_FILE = str(cache_base_dir / "cache_db")  # type: ignore

        # ------ apply cfg patches ------ #
        mocker.patch(f"{cfg.OTACLIENT_STUB_MODULE_PATH}.proxy_info", proxy_info)
        mocker.patch(
            f"{cfg.OTACLIENT_STUB_MODULE_PATH}.local_otaproxy_cfg",
            proxy_server_cfg,
        )

        # init launcher inst
        threadpool = ThreadPoolExecutor()
        self.otaproxy_launcher = OTAProxyLauncher(
            executor=threadpool,
            subprocess_ctx=_DummyOTAProxyContext(str(self.sentinel_file)),
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
    async def setup_test(self, mocker: MockerFixture, ecu_info_fixture):
        # ------ load test ecu_info.yaml ------ #
        self.ecu_info = ecu_info = ecu_info_fixture

        # ------ apply cfg patches ------ #
        mocker.patch(f"{cfg.OTACLIENT_STUB_MODULE_PATH}.ecu_info", ecu_info)

        # init and setup the ecu_storage
        self.ecu_storage = ECUStatusStorage()
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
                api_types.StatusResponseEcuV2(
                    ecu_id="autoware",
                    ota_status=api_types.StatusOta.SUCCESS,
                    firmware_version="123.x",
                    failure_type=api_types.FailureType.NO_FAILURE,
                ),
                # sub ECU's status report
                [
                    api_types.StatusResponse(
                        available_ecu_ids=["p1"],
                        ecu_v2=[
                            api_types.StatusResponseEcuV2(
                                ecu_id="p1",
                                ota_status=api_types.StatusOta.SUCCESS,
                                firmware_version="123.x",
                                failure_type=api_types.FailureType.NO_FAILURE,
                            )
                        ],
                    ),
                    api_types.StatusResponse(
                        available_ecu_ids=["p2"],
                        ecu=[
                            api_types.StatusResponseEcu(
                                ecu_id="p2",
                                result=api_types.FailureType.NO_FAILURE,
                                status=api_types.Status(
                                    status=api_types.StatusOta.SUCCESS,
                                    version="123.x",
                                ),
                            ),
                        ],
                    ),
                ],
                # expected export
                api_types.StatusResponse(
                    available_ecu_ids=["autoware", "p1", "p2"],
                    # explicitly v1 format compatibility
                    ecu=[
                        api_types.StatusResponseEcu(
                            ecu_id="autoware",
                            result=api_types.FailureType.NO_FAILURE,
                            status=api_types.Status(
                                status=api_types.StatusOta.SUCCESS,
                                version="123.x",
                            ),
                        ),
                        api_types.StatusResponseEcu(
                            ecu_id="p1",
                            result=api_types.FailureType.NO_FAILURE,
                            status=api_types.Status(
                                status=api_types.StatusOta.SUCCESS,
                                version="123.x",
                            ),
                        ),
                        api_types.StatusResponseEcu(
                            ecu_id="p2",
                            result=api_types.FailureType.NO_FAILURE,
                            status=api_types.Status(
                                status=api_types.StatusOta.SUCCESS,
                                version="123.x",
                            ),
                        ),
                    ],
                    ecu_v2=[
                        api_types.StatusResponseEcuV2(
                            ecu_id="autoware",
                            ota_status=api_types.StatusOta.SUCCESS,
                            failure_type=api_types.FailureType.NO_FAILURE,
                            firmware_version="123.x",
                        ),
                        api_types.StatusResponseEcuV2(
                            ecu_id="p1",
                            ota_status=api_types.StatusOta.SUCCESS,
                            failure_type=api_types.FailureType.NO_FAILURE,
                            firmware_version="123.x",
                        ),
                    ],
                ),
            ),  # case 1
            # case 2
            (
                # local ecu status report
                api_types.StatusResponseEcuV2(
                    ecu_id="autoware",
                    ota_status=api_types.StatusOta.UPDATING,
                    firmware_version="123.x",
                    failure_type=api_types.FailureType.NO_FAILURE,
                    update_status=api_types.UpdateStatus(
                        update_firmware_version="789.x",
                        phase=api_types.UpdatePhase.DOWNLOADING_OTA_FILES,
                        total_elapsed_time=api_types.Duration(seconds=123),
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
                    api_types.StatusResponse(
                        available_ecu_ids=["p1"],
                        ecu_v2=[
                            api_types.StatusResponseEcuV2(
                                ecu_id="p1",
                                ota_status=api_types.StatusOta.UPDATING,
                                firmware_version="123.x",
                                failure_type=api_types.FailureType.NO_FAILURE,
                                update_status=api_types.UpdateStatus(
                                    update_firmware_version="789.x",
                                    phase=api_types.UpdatePhase.DOWNLOADING_OTA_FILES,
                                    total_elapsed_time=api_types.Duration(seconds=123),
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
                    api_types.StatusResponse(
                        available_ecu_ids=["p2"],
                        ecu=[
                            api_types.StatusResponseEcu(
                                ecu_id="p2",
                                result=api_types.FailureType.NO_FAILURE,
                                status=api_types.Status(
                                    status=api_types.StatusOta.SUCCESS,
                                    version="123.x",
                                ),
                            ),
                        ],
                    ),
                ],
                # expected export result
                api_types.StatusResponse(
                    available_ecu_ids=["autoware", "p1", "p2"],
                    # explicitly v1 format compatibility
                    # NOTE: processed_files_num(v2) = files_processed_download(v1) + files_processed_copy(v1)
                    # check api_types.UpdateStatus.convert_to_v1_StatusProgress for more details.
                    ecu=[
                        api_types.StatusResponseEcu(
                            ecu_id="autoware",
                            result=api_types.FailureType.NO_FAILURE,
                            status=api_types.Status(
                                status=api_types.StatusOta.UPDATING,
                                version="123.x",
                                progress=api_types.StatusProgress(
                                    phase=api_types.StatusProgressPhase.REGULAR,
                                    total_regular_files=123456,
                                    files_processed_download=100,
                                    file_size_processed_download=400,
                                    files_processed_copy=23,
                                    file_size_processed_copy=56,
                                    download_bytes=789,
                                    regular_files_processed=123,
                                    total_elapsed_time=api_types.Duration(seconds=123),
                                ),
                            ),
                        ),
                        api_types.StatusResponseEcu(
                            ecu_id="p1",
                            result=api_types.FailureType.NO_FAILURE,
                            status=api_types.Status(
                                status=api_types.StatusOta.UPDATING,
                                version="123.x",
                                progress=api_types.StatusProgress(
                                    phase=api_types.StatusProgressPhase.REGULAR,
                                    total_regular_files=123456,
                                    files_processed_download=100,
                                    file_size_processed_download=400,
                                    files_processed_copy=23,
                                    file_size_processed_copy=56,
                                    download_bytes=789,
                                    regular_files_processed=123,
                                    total_elapsed_time=api_types.Duration(seconds=123),
                                ),
                            ),
                        ),
                        api_types.StatusResponseEcu(
                            ecu_id="p2",
                            result=api_types.FailureType.NO_FAILURE,
                            status=api_types.Status(
                                version="123.x",
                                status=api_types.StatusOta.SUCCESS,
                            ),
                        ),
                    ],
                    ecu_v2=[
                        api_types.StatusResponseEcuV2(
                            ecu_id="autoware",
                            ota_status=api_types.StatusOta.UPDATING,
                            failure_type=api_types.FailureType.NO_FAILURE,
                            firmware_version="123.x",
                            update_status=api_types.UpdateStatus(
                                update_firmware_version="789.x",
                                phase=api_types.UpdatePhase.DOWNLOADING_OTA_FILES,
                                total_elapsed_time=api_types.Duration(seconds=123),
                                total_files_num=123456,
                                processed_files_num=123,
                                processed_files_size=456,
                                downloaded_bytes=789,
                                downloaded_files_num=100,
                                downloaded_files_size=400,
                            ),
                        ),
                        api_types.StatusResponseEcuV2(
                            ecu_id="p1",
                            ota_status=api_types.StatusOta.UPDATING,
                            failure_type=api_types.FailureType.NO_FAILURE,
                            firmware_version="123.x",
                            update_status=api_types.UpdateStatus(
                                update_firmware_version="789.x",
                                phase=api_types.UpdatePhase.DOWNLOADING_OTA_FILES,
                                total_elapsed_time=api_types.Duration(seconds=123),
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
        local_ecu_status: api_types.StatusResponseEcuV2,
        sub_ecus_status: List[api_types.StatusResponse],
        expected: api_types.StatusResponse,
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
                api_types.StatusResponseEcuV2(
                    ecu_id="autoware",
                    ota_status=api_types.StatusOta.UPDATING,
                    update_status=api_types.UpdateStatus(
                        phase=api_types.UpdatePhase.DOWNLOADING_OTA_FILES
                    ),
                ),
                # sub ECUs status
                [
                    api_types.StatusResponse(
                        available_ecu_ids=["p1"],
                        ecu_v2=[
                            api_types.StatusResponseEcuV2(
                                ecu_id="p1",
                                ota_status=api_types.StatusOta.FAILURE,
                            ),
                        ],
                    ),
                    # p2: updating, doesn't require network
                    api_types.StatusResponse(
                        available_ecu_ids=["p2"],
                        ecu=[
                            api_types.StatusResponseEcu(
                                ecu_id="p2",
                                status=api_types.Status(
                                    status=api_types.StatusOta.UPDATING,
                                    progress=api_types.StatusProgress(
                                        phase=api_types.StatusProgressPhase.POST_PROCESSING,
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
                api_types.StatusResponseEcuV2(
                    ecu_id="autoware",
                    ota_status=api_types.StatusOta.SUCCESS,
                ),
                # sub ECUs status
                [
                    # p1: FAILURE
                    api_types.StatusResponse(
                        available_ecu_ids=["p1"],
                        ecu_v2=[
                            api_types.StatusResponseEcuV2(
                                ecu_id="p1",
                                ota_status=api_types.StatusOta.FAILURE,
                            ),
                        ],
                    ),
                    # p2: updating, requires network
                    api_types.StatusResponse(
                        available_ecu_ids=["p2"],
                        ecu=[
                            api_types.StatusResponseEcu(
                                ecu_id="p2",
                                status=api_types.Status(
                                    status=api_types.StatusOta.UPDATING,
                                    progress=api_types.StatusProgress(
                                        phase=api_types.StatusProgressPhase.REGULAR,
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
        local_ecu_status: api_types.StatusResponseEcuV2,
        sub_ecus_status: List[api_types.StatusResponse],
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
                api_types.StatusResponseEcuV2(
                    ecu_id="autoware",
                    ota_status=api_types.StatusOta.FAILURE,
                ),
                # sub ECUs status
                [
                    # p1: FAILED
                    api_types.StatusResponse(
                        available_ecu_ids=["p1"],
                        ecu_v2=[
                            api_types.StatusResponseEcuV2(
                                ecu_id="p1",
                                ota_status=api_types.StatusOta.FAILURE,
                            ),
                        ],
                    ),
                    # p2: UPDATING
                    api_types.StatusResponse(
                        available_ecu_ids=["p2"],
                        ecu=[
                            api_types.StatusResponseEcu(
                                ecu_id="p2",
                                status=api_types.Status(
                                    status=api_types.StatusOta.UPDATING,
                                    progress=api_types.StatusProgress(
                                        phase=api_types.StatusProgressPhase.REGULAR,
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
                api_types.StatusResponseEcuV2(
                    ecu_id="autoware",
                    ota_status=api_types.StatusOta.UPDATING,
                    update_status=api_types.UpdateStatus(
                        phase=api_types.UpdatePhase.DOWNLOADING_OTA_FILES,
                    ),
                ),
                # sub ECUs status
                [
                    # p1: FAILED
                    api_types.StatusResponse(
                        available_ecu_ids=["p1"],
                        ecu_v2=[
                            api_types.StatusResponseEcuV2(
                                ecu_id="p1",
                                ota_status=api_types.StatusOta.FAILURE,
                            ),
                        ],
                    ),
                    # p2: SUCCESS
                    api_types.StatusResponse(
                        available_ecu_ids=["p2"],
                        ecu=[
                            api_types.StatusResponseEcu(
                                ecu_id="p2",
                                status=api_types.Status(
                                    status=api_types.StatusOta.SUCCESS,
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
        local_ecu_status: api_types.StatusResponseEcuV2,
        sub_ecus_status: List[api_types.StatusResponse],
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
        return api_types.UpdateResponse(
            ecu=[
                api_types.UpdateResponseEcu(
                    ecu_id=ecu_id, result=api_types.FailureType.NO_FAILURE
                )
            ]
        )

    @pytest.fixture(autouse=True)
    async def setup_test(
        self, mocker: MockerFixture, ecu_info_fixture, proxy_info_fixture
    ):
        threadpool = ThreadPoolExecutor()

        # ------ mock and patch ecu_info ------ #
        self.ecu_info = ecu_info = ecu_info_fixture
        mocker.patch(f"{cfg.OTACLIENT_STUB_MODULE_PATH}.ecu_info", ecu_info)

        # ------ init and setup the ecu_storage ------ #
        self.ecu_storage = ECUStatusStorage()
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
        self.otaclient_api_types = mocker.MagicMock(spec=OTAServicer)
        self.ecu_status_tracker = mocker.MagicMock()
        self.otaproxy_launcher = mocker.MagicMock(spec=OTAProxyLauncher)
        # mock OTAClientCall, make update_call return success on any update dispatches to subECUs
        self.otaclient_call = mocker.AsyncMock(spec=OTAClientCall)
        self.otaclient_call.update_call = mocker.AsyncMock(
            wraps=self._subecu_accept_update_request
        )

        # ------ mock and patch proxy_info ------ #
        self.proxy_info = proxy_info = proxy_info_fixture
        mocker.patch(f"{cfg.OTACLIENT_STUB_MODULE_PATH}.proxy_info", proxy_info)

        # --- patching and mocking --- #
        mocker.patch(
            f"{cfg.OTACLIENT_STUB_MODULE_PATH}.ECUStatusStorage",
            mocker.MagicMock(return_value=self.ecu_storage),
        )
        mocker.patch(
            f"{cfg.OTACLIENT_STUB_MODULE_PATH}.OTAServicer",
            mocker.MagicMock(return_value=self.otaclient_api_types),
        )
        mocker.patch(
            f"{cfg.OTACLIENT_STUB_MODULE_PATH}._ECUTracker",
            mocker.MagicMock(return_value=self.ecu_status_tracker),
        )
        mocker.patch(
            f"{cfg.OTACLIENT_STUB_MODULE_PATH}.OTAProxyLauncher",
            mocker.MagicMock(return_value=self.otaproxy_launcher),
        )
        mocker.patch(
            f"{cfg.OTACLIENT_STUB_MODULE_PATH}.OTAClientCall", self.otaclient_call
        )

        # --- start the OTAClientServiceStub --- #
        self.otaclient_service_stub = OTAClientServiceStub()

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
                api_types.UpdateRequest(
                    ecu=[
                        api_types.UpdateRequestEcu(
                            ecu_id="autoware",
                            version="789.x",
                            url="url",
                            cookies="cookies",
                        ),
                        api_types.UpdateRequestEcu(
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
                api_types.UpdateResponse(
                    ecu=[
                        api_types.UpdateResponseEcu(
                            ecu_id="p1",
                            result=api_types.FailureType.NO_FAILURE,
                        ),
                        api_types.UpdateResponseEcu(
                            ecu_id="autoware",
                            result=api_types.FailureType.NO_FAILURE,
                        ),
                    ]
                ),
            ),
            # update only p2
            (
                api_types.UpdateRequest(
                    ecu=[
                        api_types.UpdateRequestEcu(
                            ecu_id="p2",
                            version="789.x",
                            url="url",
                            cookies="cookies",
                        ),
                    ]
                ),
                {"p2"},
                api_types.UpdateResponse(
                    ecu=[
                        api_types.UpdateResponseEcu(
                            ecu_id="p2",
                            result=api_types.FailureType.NO_FAILURE,
                        ),
                    ]
                ),
            ),
        ),
    )
    async def test_update_normal(
        self,
        update_request: api_types.UpdateRequest,
        update_target_ids: Set[str],
        expected: api_types.UpdateResponse,
    ):
        # --- setup --- #
        self.otaclient_api_types.dispatch_update.return_value = (
            api_types.UpdateResponseEcu(
                ecu_id=self.ecu_info.ecu_id, result=api_types.FailureType.NO_FAILURE
            )
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
        self.otaclient_api_types.dispatch_update.return_value = (
            api_types.UpdateResponseEcu(
                ecu_id="autoware", result=api_types.FailureType.RECOVERABLE
            )
        )
        update_request_ecu = api_types.UpdateRequestEcu(
            ecu_id="autoware", version="version", url="url", cookies="cookies"
        )

        # --- execution --- #
        resp = await self.otaclient_service_stub.update(
            api_types.UpdateRequest(ecu=[update_request_ecu])
        )

        # --- assertion --- #
        assert resp == api_types.UpdateResponse(
            ecu=[
                api_types.UpdateResponseEcu(
                    ecu_id="autoware",
                    result=api_types.FailureType.RECOVERABLE,
                )
            ]
        )
        self.otaclient_api_types.dispatch_update.assert_called_once_with(
            update_request_ecu
        )
