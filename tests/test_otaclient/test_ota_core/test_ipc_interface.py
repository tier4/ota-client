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

from queue import Queue

import pytest
import pytest_mock

from otaclient import ota_core
from otaclient._status_monitor import (
    OTAClientStatusCollector,
    StatusReport,
)
from otaclient._types import OTAStatus, UpdateRequestV2
from otaclient.boot_control import BootControllerProtocol
from otaclient.errors import OTAErrorRecoverable
from otaclient.ota_core import OTAClient, OTAClientUpdater, OTAUpdaterForLegacyOTAImage

OTA_CORE_MAIN_MODULE = ota_core._main.__name__


class TestOTAClient:
    """Testing on OTAClient workflow."""

    OTACLIENT_VERSION = "otaclient_version"
    CURRENT_FIRMWARE_VERSION = "firmware_version"
    UPDATE_FIRMWARE_VERSION = "update_firmware_version"

    UPDATE_COOKIES_JSON = r'{"test": "my-cookie"}'
    OTA_IMAGE_URL = "url"
    MY_ECU_ID = "autoware"

    @pytest.fixture(autouse=True)
    def mock_setup(
        self,
        ota_status_collector: tuple[OTAClientStatusCollector, Queue[StatusReport]],
        mocker: pytest_mock.MockerFixture,
    ):
        _, status_report_queue = ota_status_collector
        ecu_status_flags = mocker.MagicMock()
        ecu_status_flags.any_child_ecu_in_update.is_set = mocker.MagicMock(
            return_value=False
        )
        client_update_control_flags = mocker.MagicMock()
        critical_zone_flag = mocker.MagicMock()

        # --- mock setup --- #
        self.control_flags = ecu_status_flags
        self.ota_updater = mocker.MagicMock(spec=OTAUpdaterForLegacyOTAImage)
        self.ota_client_updater = mocker.MagicMock(spec=OTAClientUpdater)

        self.boot_controller = mocker.MagicMock(spec=BootControllerProtocol)

        # patch boot_controller for otaclient initializing
        self.boot_controller.load_version.return_value = self.CURRENT_FIRMWARE_VERSION
        self.boot_controller.get_booted_ota_status = mocker.MagicMock(
            return_value=OTAStatus.SUCCESS
        )

        # patch inject mocked updater
        mocker.patch(
            f"{OTA_CORE_MAIN_MODULE}.OTAUpdaterForLegacyOTAImage",
            return_value=self.ota_updater,
        )
        mocker.patch(
            f"{OTA_CORE_MAIN_MODULE}.OTAClientUpdater",
            return_value=self.ota_client_updater,
        )
        mocker.patch(
            f"{OTA_CORE_MAIN_MODULE}.get_boot_controller",
            return_value=self.boot_controller,
        )

        # start otaclient
        self.ota_client = OTAClient(
            ecu_status_flags=ecu_status_flags,
            status_report_queue=status_report_queue,
            client_update_control_flags=client_update_control_flags,
            critical_zone_flag=critical_zone_flag,
            shm_metrics_reader=mocker.MagicMock(),
        )

    def test_update_normal_finished(self, mocker: pytest_mock.MockerFixture):
        mock_publish = mocker.patch.object(type(self.ota_client._metrics), "publish")

        # --- execution --- #
        self.ota_client.update(
            request=UpdateRequestV2(
                version=self.UPDATE_FIRMWARE_VERSION,
                url_base=self.OTA_IMAGE_URL,
                cookies_json=self.UPDATE_COOKIES_JSON,
                request_id="test-request-id",
                session_id="test_update_normal_finished",
            )
        )

        # --- assert on update finished(before reboot) --- #
        self.ota_updater.execute.assert_called_once()
        assert self.ota_client.live_ota_status == OTAStatus.UPDATING

        # publish should be called twice: once for REQUEST and once for UPDATE
        assert mock_publish.call_count == 2

    def test_update_interrupted(self, mocker: pytest_mock.MockerFixture):
        mock_exit_from_dynamic_client = mocker.patch.object(
            self.ota_client, "_exit_from_dynamic_client"
        )
        mock_publish = mocker.patch.object(type(self.ota_client._metrics), "publish")

        # inject exception
        _error = OTAErrorRecoverable("interrupted by test as expected", module=__name__)
        self.ota_updater.execute.side_effect = _error

        # --- execution --- #
        self.ota_client.update(
            request=UpdateRequestV2(
                version=self.UPDATE_FIRMWARE_VERSION,
                url_base=self.OTA_IMAGE_URL,
                cookies_json=self.UPDATE_COOKIES_JSON,
                request_id="test-request-id",
                session_id="test_update_interrupted",
            )
        )

        # --- assertion on interrupted OTA update --- #
        self.ota_updater.execute.assert_called_once()
        assert self.ota_client.live_ota_status == OTAStatus.FAILURE

        # publish should be called twice: once for REQUEST and once for UPDATE
        assert mock_publish.call_count == 2
        mock_exit_from_dynamic_client.assert_called_once()

    def test_client_update_normal_finished(self):
        """Test client update with normal completion."""
        from otaclient._types import ClientUpdateRequestV2

        # --- execution --- #
        self.ota_client.client_update(
            request=ClientUpdateRequestV2(
                version=self.UPDATE_FIRMWARE_VERSION,
                url_base=self.OTA_IMAGE_URL,
                cookies_json=self.UPDATE_COOKIES_JSON,
                request_id="test-request-id",
                session_id="test_client_update_normal_finished",
            )
        )

        # --- assert on update finished --- #
        self.ota_client_updater.execute.assert_called_once()
        assert self.ota_client.live_ota_status == OTAStatus.CLIENT_UPDATING

    def test_client_update_interrupted(self, mocker: pytest_mock.MockerFixture):
        """Test client update with interruption."""
        from otaclient._types import ClientUpdateRequestV2

        # inject exception
        _error = OTAErrorRecoverable(
            "client update interrupted by test as expected", module=__name__
        )
        self.ota_client_updater.execute.side_effect = _error
        self.ota_client._client_update_control_flags.request_shutdown_event.set = (
            mocker.MagicMock()
        )

        # --- execution --- #
        self.ota_client.client_update(
            request=ClientUpdateRequestV2(
                version=self.UPDATE_FIRMWARE_VERSION,
                url_base=self.OTA_IMAGE_URL,
                cookies_json=self.UPDATE_COOKIES_JSON,
                request_id="test-request-id",
                session_id="test_client_update_interrupted",
            )
        )

        # --- assertion on interrupted client update --- #
        self.ota_client_updater.execute.assert_called_once()
        assert self.ota_client.live_ota_status == OTAStatus.SUCCESS
