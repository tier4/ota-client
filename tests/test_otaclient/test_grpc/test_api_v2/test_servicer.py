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
from typing import Set

import pytest
from pytest_mock import MockerFixture

from otaclient.configs._ecu_info import ECUInfo
from otaclient.configs._proxy_info import ProxyInfo
from otaclient.grpc.api_v2 import ecu_status, servicer
from otaclient.grpc.api_v2.ecu_tracker import ECUTracker
from otaclient.grpc.api_v2.servicer import (
    ECUStatusStorage,
    OTAClientAPIServicer,
    OTAProxyLauncher,
)
from otaclient.grpc.api_v2.types import convert_from_apiv2_update_request
from otaclient.ota_core import OTAClient, OTAClientControlFlags
from otaclient_api.v2 import types as api_types
from otaclient_api.v2.api_caller import OTAClientCall
from tests.utils import compare_message

logger = logging.getLogger(__name__)

SERVICER_MODULE = servicer.__name__
ECU_STATUS_MODULE = ecu_status.__name__


class TestOTAClientServiceStub:
    POLLING_INTERVAL = 1
    ENSURE_NEXT_CHECKING_ROUND = 1.2

    @staticmethod
    async def _subecu_accept_update_request(
        ecu_id, *args, **kwargs
    ) -> api_types.UpdateResponse:
        return api_types.UpdateResponse(
            ecu=[
                api_types.UpdateResponseEcu(
                    ecu_id=ecu_id, result=api_types.FailureType.NO_FAILURE
                )
            ]
        )

    @pytest.fixture(autouse=True)
    async def setup_test(
        self,
        mocker: MockerFixture,
        ecu_info_fixture: ECUInfo,
        proxy_info_fixture: ProxyInfo,
    ):
        threadpool = ThreadPoolExecutor()

        # ------ mock and patch ------ #
        self.ecu_info = ecu_info = ecu_info_fixture
        mocker.patch(f"{SERVICER_MODULE}.ecu_info", ecu_info)

        # NOTE: decrease the interval to speed up testing
        #       (used by _otaproxy_lifecycle_managing/_otaclient_control_flags_managing task)
        mocker.patch(
            f"{ECU_STATUS_MODULE}.ACTIVE_POLLING_INTERVAL", self.POLLING_INTERVAL
        )
        mocker.patch(
            f"{ECU_STATUS_MODULE}.IDLE_POLLING_INTERVAL", self.POLLING_INTERVAL
        )

        # ------ init and setup the ecu_storage ------ #
        self.control_flag = OTAClientControlFlags()
        self.ecu_storage = ECUStatusStorage()
        self.ecu_storage.on_ecus_accept_update_request = mocker.AsyncMock()
        # NOTE: disable internal overall ecu status generation task as we
        #       will manipulate the values by ourselves.
        self.ecu_storage._debug_properties_update_shutdown_event.set()
        await asyncio.sleep(self.ENSURE_NEXT_CHECKING_ROUND)  # ensure the task stopping

        # --- mocker --- #
        self.otaclient_inst = mocker.MagicMock(spec=OTAClient)
        type(self.otaclient_inst).started = mocker.PropertyMock(return_value=True)
        type(self.otaclient_inst).is_busy = mocker.PropertyMock(return_value=False)

        self.ecu_status_tracker = mocker.MagicMock(spec=ECUTracker)
        self.otaproxy_launcher = mocker.MagicMock(spec=OTAProxyLauncher)
        # mock OTAClientCall, make update_call return success on any update dispatches to subECUs
        self.otaclient_call = mocker.AsyncMock(spec=OTAClientCall)
        self.otaclient_call.update_call = mocker.AsyncMock(
            wraps=self._subecu_accept_update_request
        )

        # ------ mock and patch proxy_info ------ #
        self.proxy_info = proxy_info = proxy_info_fixture
        mocker.patch(f"{SERVICER_MODULE}.proxy_info", proxy_info)

        # --- patching and mocking --- #
        mocker.patch(
            f"{SERVICER_MODULE}.ECUStatusStorage",
            mocker.MagicMock(return_value=self.ecu_storage),
        )
        mocker.patch(
            f"{SERVICER_MODULE}.OTAProxyLauncher",
            mocker.MagicMock(return_value=self.otaproxy_launcher),
        )
        mocker.patch(f"{SERVICER_MODULE}.OTAClientCall", self.otaclient_call)

        # --- start the OTAClientServiceStub --- #
        self.otaclient_service_stub = OTAClientAPIServicer(
            otaclient_inst=self.otaclient_inst,
            ecu_status_storage=self.ecu_storage,
            control_flag=self.control_flag,
            executor=threadpool,
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
        otaclient_control_flags = self.control_flag
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
        # --- execution --- #
        resp = await self.otaclient_service_stub.update(update_request)

        # --- assertion --- #
        compare_message(resp, expected)

        self.otaclient_call.update_call.assert_called()
        self.ecu_storage.on_ecus_accept_update_request.assert_called_once_with(  # type: ignore
            update_target_ids
        )
        # assert otaclient_inst receives the update request if we have update request for self ECU
        if update_request.if_contains_ecu("autoware"):
            _update_request_ecu = update_request.find_ecu("autoware")
            assert _update_request_ecu

            self.otaclient_inst.update.assert_called_once_with(
                convert_from_apiv2_update_request(_update_request_ecu)
            )

    async def test_update_local_ecu_busy(
        self,
        mocker: MockerFixture,
    ):
        # --- preparation --- #
        is_busy_mock = mocker.PropertyMock(return_value=True)  # is busy
        type(self.otaclient_inst).is_busy = is_busy_mock

        update_request_ecu = api_types.UpdateRequestEcu(
            ecu_id="autoware", version="version", url="url", cookies="cookies"
        )

        # --- execution --- #
        await self.otaclient_service_stub.update(
            api_types.UpdateRequest(ecu=[update_request_ecu])
        )

        # --- assertion --- #
        # assert otaclient_inst doesn't receive the update request
        self.otaclient_inst.update.assert_not_called()
