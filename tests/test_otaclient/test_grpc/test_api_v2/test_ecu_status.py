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
import threading
from typing import Any

import pytest
import pytest_mock
from pytest_mock import MockerFixture

from otaclient import __version__
from otaclient import _types as _internal_types
from otaclient._types import MultipleECUStatusFlags
from otaclient.configs import DefaultOTAClientConfigs
from otaclient.configs._ecu_info import ECUInfo
from otaclient.grpc.api_v2.servicer import ECUStatusStorage
from otaclient_api.v2 import _types as api_types
from tests.utils import compare_message

logger = logging.getLogger(__name__)

ECU_STATUS_MODULE = "otaclient.grpc.api_v2.ecu_status"


class TestECUStatusStorage:
    PROPERTY_REFRESH_INTERVAL_FOR_TEST = 1
    SAFE_INTERVAL_FOR_PROPERTY_UPDATE = 2

    @pytest.fixture(autouse=True)
    async def setup_test(self, mocker: MockerFixture, ecu_info_fixture: ECUInfo):
        # ------ load test ecu_info.yaml ------ #
        self.ecu_info = ecu_info = ecu_info_fixture

        # ------ apply cfg patches ------ #
        mocker.patch(f"{ECU_STATUS_MODULE}.ecu_info", ecu_info)

        # init and setup the ecu_storage
        # NOTE: here we use threading.Event instead
        self.ecu_status_flags = ecu_status_flags = MultipleECUStatusFlags(
            any_child_ecu_in_update=threading.Event(),  # type: ignore[assignment]
            any_requires_network=threading.Event(),  # type: ignore[assignment]
            all_success=threading.Event(),  # type: ignore[assignment]
        )
        self.ecu_storage = ECUStatusStorage(ecu_status_flags=ecu_status_flags)

        _mocked_otaclient_cfg = DefaultOTAClientConfigs()
        # NOTE: decrease the interval for faster testing
        _mocked_otaclient_cfg.OVERALL_ECUS_STATUS_UPDATE_INTERVAL = self.PROPERTY_REFRESH_INTERVAL_FOR_TEST  # type: ignore[assignment]
        mocker.patch(f"{ECU_STATUS_MODULE}.cfg", _mocked_otaclient_cfg)

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
                _internal_types.OTAClientStatus(
                    ota_status=_internal_types.OTAStatus.SUCCESS,
                    firmware_version="123.x",
                    failure_type=_internal_types.FailureType.NO_FAILURE,
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
                            otaclient_version=__version__,
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
                _internal_types.OTAClientStatus(
                    ota_status=_internal_types.OTAStatus.UPDATING,
                    firmware_version="123.x",
                    failure_type=_internal_types.FailureType.NO_FAILURE,
                    update_phase=_internal_types.UpdatePhase.DOWNLOADING_OTA_FILES,
                    update_meta=_internal_types.UpdateMeta(
                        update_firmware_version="789.x",
                        image_file_entries=123456,
                        image_size_uncompressed=123456,
                    ),
                    update_progress=_internal_types.UpdateProgress(
                        downloaded_files_num=100,
                        downloaded_files_size=400,
                        downloaded_bytes=789,
                        processed_files_num=123,
                        processed_files_size=456,
                    ),
                    update_timing=_internal_types.UpdateTiming(),
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
                                # NOTE: also see convert_to_v1_StatusProgress for more details.
                                progress=api_types.StatusProgress(
                                    phase=api_types.StatusProgressPhase.REGULAR,
                                    total_regular_files=123456,
                                    total_regular_file_size=123456,
                                    files_processed_copy=23,
                                    files_processed_download=100,
                                    file_size_processed_download=400,
                                    # NOTE: processed_files_size(456) - downloaded_files_size(400)
                                    file_size_processed_copy=56,
                                    download_bytes=789,
                                    regular_files_processed=123,
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
                            otaclient_version=__version__,
                            ota_status=api_types.StatusOta.UPDATING,
                            failure_type=api_types.FailureType.NO_FAILURE,
                            firmware_version="123.x",
                            update_status=api_types.UpdateStatus(
                                update_firmware_version="789.x",
                                phase=api_types.UpdatePhase.DOWNLOADING_OTA_FILES,
                                total_files_num=123456,
                                total_files_size_uncompressed=123456,
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
        local_ecu_status: _internal_types.OTAClientStatus,
        sub_ecus_status: list[api_types.StatusResponse],
        expected: api_types.StatusResponse,
    ):
        # --- prepare --- #
        await self.ecu_storage.update_from_local_ecu(local_ecu_status)
        for ecu_status_report in sub_ecus_status:
            await self.ecu_storage.update_from_child_ecu(ecu_status_report)

        await asyncio.sleep(
            self.SAFE_INTERVAL_FOR_PROPERTY_UPDATE
        )  # wait for status report generation  # wait for status updated

        # --- execution --- #
        exported = await self.ecu_storage.export()

        # ---  assertion --- #
        compare_message(exported, expected)

    @pytest.mark.parametrize(
        "local_ecu_status,sub_ecus_status,properties_dict,flags_status",
        (
            # case 1:
            (
                # local ECU status: UPDATING, requires network
                _internal_types.OTAClientStatus(
                    ota_status=_internal_types.OTAStatus.UPDATING,
                    update_phase=_internal_types.UpdatePhase.DOWNLOADING_OTA_FILES,
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
                    "success_ecus_id": set(),
                },
                # ecu_status_flags
                {
                    "any_child_ecu_in_update": True,
                    "any_requires_network": True,
                    "all_success": False,
                },
            ),
            # case 2:
            (
                # local ECU status: SUCCESS
                _internal_types.OTAClientStatus(
                    ota_status=_internal_types.OTAStatus.SUCCESS
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
                    "success_ecus_id": {"autoware"},
                },
                # ecu_status_flags
                {
                    "any_child_ecu_in_update": True,
                    "any_requires_network": True,
                    "all_success": False,
                },
            ),
            # case 3:
            # only main ECU doing OTA update.
            (
                # local ECU status: UPDATING
                _internal_types.OTAClientStatus(
                    ota_status=_internal_types.OTAStatus.UPDATING,
                    update_phase=_internal_types.UpdatePhase.DOWNLOADING_OTA_FILES,
                ),
                # sub ECUs status
                [
                    # p1: SUCCESS
                    api_types.StatusResponse(
                        available_ecu_ids=["p1"],
                        ecu_v2=[
                            api_types.StatusResponseEcuV2(
                                ecu_id="p1",
                                ota_status=api_types.StatusOta.SUCCESS,
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
                # expected overal ECUs status report set by on_ecus_accept_update_request,
                {
                    "lost_ecus_id": set(),
                    "in_update_ecus_id": {"autoware"},
                    "in_update_child_ecus_id": set(),
                    "failed_ecus_id": set(),
                    "success_ecus_id": {"p1", "p2"},
                },
                # ecu_status_flags
                {
                    "any_child_ecu_in_update": False,
                    "any_requires_network": True,
                    "all_success": False,
                },
            ),
        ),
    )
    async def test_overall_ecu_status_report_generation(
        self,
        local_ecu_status: _internal_types.OTAClientStatus,
        sub_ecus_status: list[api_types.StatusResponse],
        properties_dict: dict[str, Any],
        flags_status: dict[str, bool],
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
            assert (
                getattr(self.ecu_storage._state, k) == v
            ), f"status_report attr {k} mismatch"

        for k, v in flags_status.items():
            assert getattr(self.ecu_status_flags, k).is_set() == v

    @pytest.mark.parametrize(
        "local_ecu_status,sub_ecus_status,ecus_accept_update_request,properties_dict,flags_status",
        (
            # case 1:
            #   There is FAILED/UPDATING ECUs existed in the cluster.
            #   We try to retry OTA update only on failed autoware ECU.
            #   on_ecus_accept_update_request should only change the overall status report
            #   based on the status change of ECUs that accept update request.
            (
                # local ECU status: FAILED
                _internal_types.OTAClientStatus(
                    ota_status=_internal_types.OTAStatus.FAILURE
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
                    "success_ecus_id": set(),
                },
                # ecu_status_flags
                {
                    "any_child_ecu_in_update": True,
                    "any_requires_network": True,
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
                _internal_types.OTAClientStatus(
                    ota_status=_internal_types.OTAStatus.UPDATING,
                    update_phase=_internal_types.UpdatePhase.DOWNLOADING_OTA_FILES,
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
                    "success_ecus_id": {"p2"},
                },
                # ecu_status_flags
                {
                    "any_child_ecu_in_update": True,
                    "any_requires_network": True,
                    "all_success": False,
                },
            ),
        ),
    )
    async def test_on_receive_update_request(
        self,
        local_ecu_status: _internal_types.OTAClientStatus,
        sub_ecus_status: list[api_types.StatusResponse],
        ecus_accept_update_request: list[str],
        properties_dict: dict[str, Any],
        flags_status: dict[str, bool],
        mocker: pytest_mock.MockerFixture,
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
        _mock_otaclient_cfg = DefaultOTAClientConfigs()
        _mock_otaclient_cfg.PAUSED_OVERALL_ECUS_STATUS_CHANGE_ON_UPDATE_REQ_ACKED = 999
        mocker.patch(f"{ECU_STATUS_MODULE}.cfg", _mock_otaclient_cfg)

        await self.ecu_storage.on_ecus_accept_update_request(
            set(ecus_accept_update_request)
        )

        # --- assertion --- #
        for k, v in properties_dict.items():
            assert (
                getattr(self.ecu_storage._state, k) == v
            ), f"status_report attr {k} mismatch"

        for k, v in flags_status.items():
            assert getattr(self.ecu_status_flags, k).is_set() == v

    async def test_polling_waiter_switching_from_idling_to_active(
        self, mocker: pytest_mock.MockerFixture
    ):
        """Waiter should immediately return if active_ota_update_present is set."""
        _sleep_time, _mocked_interval = 3, 60

        mocker.patch(f"{ECU_STATUS_MODULE}.IDLE_POLLING_INTERVAL", _mocked_interval)

        async def _event_setter():
            await asyncio.sleep(_sleep_time)
            await self.ecu_storage.on_ecus_accept_update_request({"autoware"})

        _waiter = self.ecu_storage.get_polling_waiter()
        asyncio.create_task(_event_setter())
        # waiter should return on active_ota_update_present is set, instead of waiting the
        #   full <IDLE_POLLING_INTERVAL>.
        # expected behavior:
        #   1. wait until _event_setter finished, or with a little bit delay.
        #   2. wait much less time than <_mocked_interval>.
        await asyncio.wait_for(_waiter(), timeout=_sleep_time + 1)
