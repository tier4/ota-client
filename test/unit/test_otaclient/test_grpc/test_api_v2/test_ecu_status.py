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
import threading
from pathlib import Path
from typing import Any

import pytest
from pytest_mock import MockerFixture

from otaclient import _types as _internal_types
from otaclient._types import MultipleECUStatusFlags
from otaclient.configs import DefaultOTAClientConfigs
from otaclient.configs._ecu_info import ECUInfo, parse_ecu_info
from otaclient.grpc.api_v2.ecu_status import (
    ACTIVE_POLLING_INTERVAL,
    IDLE_POLLING_INTERVAL,
    LocalECUStatusNotReady,
)
from otaclient.grpc.api_v2.servicer import ECUStatusStorage
from otaclient_api.v2 import _types as api_types

ECU_STATUS_MODULE = "otaclient.grpc.api_v2.ecu_status"

ECU_INFO_YAML = """\
format_version: 1
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
    - "p1"
    - "p2"
"""


@pytest.fixture
def ecu_info_fixture(tmp_path: Path) -> ECUInfo:
    _yaml_f = tmp_path / "ecu_info.yaml"
    _yaml_f.write_text(ECU_INFO_YAML)
    _, res = parse_ecu_info(_yaml_f)
    return res


class TestECUStatusStorageAbortingStatus:
    """Tests for ABORTING status handling in ECUStatusStorage."""

    PROPERTY_REFRESH_INTERVAL_FOR_TEST = 1
    SAFE_INTERVAL_FOR_PROPERTY_UPDATE = 2

    @pytest.fixture(autouse=True)
    async def setup_test(self, mocker: MockerFixture, ecu_info_fixture: ECUInfo):
        self.ecu_info = ecu_info = ecu_info_fixture

        mocker.patch(f"{ECU_STATUS_MODULE}.ecu_info", ecu_info)

        self.ecu_status_flags = ecu_status_flags = MultipleECUStatusFlags(
            any_child_ecu_in_update=threading.Event(),  # type: ignore[assignment]
            any_requires_network=threading.Event(),  # type: ignore[assignment]
            all_success=threading.Event(),  # type: ignore[assignment]
        )
        self.ecu_storage = ECUStatusStorage(ecu_status_flags=ecu_status_flags)

        _mocked_otaclient_cfg = DefaultOTAClientConfigs()
        _mocked_otaclient_cfg.OVERALL_ECUS_STATUS_UPDATE_INTERVAL = (
            self.PROPERTY_REFRESH_INTERVAL_FOR_TEST
        )  # type: ignore[assignment]
        mocker.patch(f"{ECU_STATUS_MODULE}.cfg", _mocked_otaclient_cfg)

        try:
            yield
        finally:
            self.ecu_storage._debug_properties_update_shutdown_event.set()
            await asyncio.sleep(self.SAFE_INTERVAL_FOR_PROPERTY_UPDATE)

    @pytest.mark.parametrize(
        "local_ecu_status,sub_ecus_status,properties_dict,flags_status",
        (
            # ECU in ABORTING status should be tracked in in_update_ecus_id.
            (
                # local ECU status: ABORTING
                _internal_types.OTAClientStatus(
                    ota_status=_internal_types.OTAStatus.ABORTING,
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
                    # p2: ABORTING
                    api_types.StatusResponse(
                        available_ecu_ids=["p2"],
                        ecu_v2=[
                            api_types.StatusResponseEcuV2(
                                ecu_id="p2",
                                ota_status=api_types.StatusOta.ABORTING,
                            ),
                        ],
                    ),
                ],
                # expected overall ECUs status report
                {
                    "lost_ecus_id": set(),
                    "in_update_ecus_id": {"autoware", "p2"},
                    "in_update_child_ecus_id": {"p2"},
                    "failed_ecus_id": set(),
                    "success_ecus_id": {"p1"},
                },
                # ecu_status_flags
                {
                    "any_child_ecu_in_update": True,
                    "any_requires_network": False,
                    "all_success": False,
                },
            ),
        ),
    )
    async def test_aborting_ecu_tracked_in_update_ecus(
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
            assert getattr(self.ecu_storage._state, k) == v, (
                f"status_report attr {k} mismatch"
            )

        for k, v in flags_status.items():
            assert getattr(self.ecu_status_flags, k).is_set() == v

    async def test_polling_interval_active_when_aborting(self):
        """get_polling_interval should return ACTIVE_POLLING_INTERVAL when an ECU is aborting."""
        aborting_status = api_types.StatusResponse(
            available_ecu_ids=["p1"],
            ecu_v2=[
                api_types.StatusResponseEcuV2(
                    ecu_id="p1",
                    ota_status=api_types.StatusOta.ABORTING,
                ),
            ],
        )
        await self.ecu_storage.update_from_child_ecu(aborting_status)
        await asyncio.sleep(self.SAFE_INTERVAL_FOR_PROPERTY_UPDATE)

        assert self.ecu_storage.get_polling_interval() == ACTIVE_POLLING_INTERVAL

    async def test_polling_interval_idle_when_no_active_or_aborting(self):
        """get_polling_interval should return IDLE_POLLING_INTERVAL when no ECU is updating or aborting."""
        success_status = api_types.StatusResponse(
            available_ecu_ids=["p1"],
            ecu_v2=[
                api_types.StatusResponseEcuV2(
                    ecu_id="p1",
                    ota_status=api_types.StatusOta.SUCCESS,
                ),
            ],
        )
        await self.ecu_storage.update_from_child_ecu(success_status)
        await asyncio.sleep(self.SAFE_INTERVAL_FOR_PROPERTY_UPDATE)

        assert self.ecu_storage.get_polling_interval() == IDLE_POLLING_INTERVAL


class TestECUStatusStorageExportLocalECUNotReady:
    """Tests for export() gating during the local ECU startup race."""

    @pytest.fixture(autouse=True)
    async def setup_test(self, mocker: MockerFixture, ecu_info_fixture: ECUInfo):
        self.ecu_info = ecu_info = ecu_info_fixture
        mocker.patch(f"{ECU_STATUS_MODULE}.ecu_info", ecu_info)

        self.ecu_status_flags = ecu_status_flags = MultipleECUStatusFlags(
            any_child_ecu_in_update=threading.Event(),  # type: ignore[assignment]
            any_requires_network=threading.Event(),  # type: ignore[assignment]
            all_success=threading.Event(),  # type: ignore[assignment]
        )
        self.ecu_storage = ECUStatusStorage(ecu_status_flags=ecu_status_flags)

        try:
            yield
        finally:
            self.ecu_storage._debug_properties_update_shutdown_event.set()

    async def test_export_raises_when_local_ecu_status_not_ready(self):
        """During the startup window before the local OTA core writes its first
        status into shared memory, my_ecu_id is absent from all_ecus_status_v2.
        export() must raise LocalECUStatusNotReady so the gRPC layer returns
        UNAVAILABLE instead of a response that omits the local ECU from ecu_v2.
        """
        # No update_from_local_ecu call: simulate the cold-start window.
        # A child ECU's status arriving first must not unblock export().
        await self.ecu_storage.update_from_child_ecu(
            api_types.StatusResponse(
                available_ecu_ids=["p1"],
                ecu_v2=[
                    api_types.StatusResponseEcuV2(
                        ecu_id="p1",
                        ota_status=api_types.StatusOta.SUCCESS,
                    ),
                ],
            )
        )

        with pytest.raises(LocalECUStatusNotReady):
            await self.ecu_storage.export()

    async def test_export_proxy_ecu_without_local_status_does_not_raise(self):
        """When the local ECU is configured as a proxy/aggregator (not in its
        own available_ecu_ids), missing local status is the steady state and
        export() must return normally with the child ECUs' statuses.
        """
        # Simulate proxy config: drop my_ecu_id from available_ecu_ids.
        self.ecu_storage._state.available_ecu_ids.pop(self.ecu_storage.my_ecu_id, None)

        await self.ecu_storage.update_from_child_ecu(
            api_types.StatusResponse(
                available_ecu_ids=["p1"],
                ecu_v2=[
                    api_types.StatusResponseEcuV2(
                        ecu_id="p1",
                        ota_status=api_types.StatusOta.SUCCESS,
                    ),
                ],
            )
        )

        exported = await self.ecu_storage.export()
        assert self.ecu_storage.my_ecu_id not in list(exported.available_ecu_ids)
        assert exported.find_ecu_v2("p1") is not None
        assert exported.find_ecu_v2(self.ecu_storage.my_ecu_id) is None
