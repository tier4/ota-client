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
    OTAStatusChangeReport,
    StatusReport,
)
from otaclient._types import FailureType, OTAStatus, UpdateRequestV2
from otaclient.boot_control import BootControllerProtocol
from otaclient.errors import OTAErrorRecoverable
from otaclient.ota_core import OTAClient, OTAClientUpdater, OTAUpdaterForLegacyOTAImage

OTA_CORE_MAIN_MODULE = ota_core._main.__name__
OTA_CORE_COMMON_MODULE = ota_core._common.__name__


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

        self.control_flags = ecu_status_flags
        self.ota_updater = mocker.MagicMock(spec=OTAUpdaterForLegacyOTAImage)
        self.ota_client_updater = mocker.MagicMock(spec=OTAClientUpdater)

        self.boot_controller = mocker.MagicMock(spec=BootControllerProtocol)
        self.boot_controller.load_version.return_value = self.CURRENT_FIRMWARE_VERSION
        self.boot_controller.get_booted_ota_status = mocker.MagicMock(
            return_value=OTAStatus.SUCCESS
        )

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

        self.ota_client = OTAClient(
            ecu_status_flags=ecu_status_flags,
            status_report_queue=status_report_queue,
            client_update_control_flags=client_update_control_flags,
            shm_metrics_reader=mocker.MagicMock(),
        )
        # Mock abort handler (normally created by OTAClient.main())
        self.ota_client._abort_handler = mocker.MagicMock()

    def test_update_normal_finished(self, mocker: pytest_mock.MockerFixture):
        mock_publish = mocker.patch.object(type(self.ota_client._metrics), "publish")

        self.ota_client.update(
            request=UpdateRequestV2(
                version=self.UPDATE_FIRMWARE_VERSION,
                url_base=self.OTA_IMAGE_URL,
                cookies_json=self.UPDATE_COOKIES_JSON,
                request_id="test-request-id",
                session_id="test_update_normal_finished",
            )
        )

        self.ota_updater.execute.assert_called_once()
        assert self.ota_client.live_ota_status == OTAStatus.UPDATING

        mock_publish.assert_called_once()

    def test_update_interrupted(self, mocker: pytest_mock.MockerFixture):
        mock_exit_from_dynamic_client = mocker.patch.object(
            self.ota_client, "_exit_from_dynamic_client"
        )
        mock_publish = mocker.patch.object(type(self.ota_client._metrics), "publish")

        _error = OTAErrorRecoverable("interrupted by test as expected", module=__name__)
        self.ota_updater.execute.side_effect = _error

        self.ota_client.update(
            request=UpdateRequestV2(
                version=self.UPDATE_FIRMWARE_VERSION,
                url_base=self.OTA_IMAGE_URL,
                cookies_json=self.UPDATE_COOKIES_JSON,
                request_id="test-request-id",
                session_id="test_update_interrupted",
            )
        )

        self.ota_updater.execute.assert_called_once()
        assert self.ota_client.live_ota_status == OTAStatus.FAILURE

        mock_publish.assert_called_once()
        mock_exit_from_dynamic_client.assert_called_once()
        # the failure is persisted for being reported after otaclient restarts
        self.ota_client.boot_controller.store_failure_info.assert_called_once_with(
            failure_type=_error.failure_type,
            failure_reason=_error.get_failure_reason(),
        )

    def test_restore_failure_info_on_startup(
        self,
        ota_status_collector: tuple[OTAClientStatusCollector, Queue[StatusReport]],
        mocker: pytest_mock.MockerFixture,
    ):
        _, status_report_queue = ota_status_collector
        _boot_controller = mocker.MagicMock(spec=BootControllerProtocol)
        _boot_controller.load_version.return_value = self.CURRENT_FIRMWARE_VERSION
        _boot_controller.load_version_detail.return_value = None
        _boot_controller.get_booted_ota_status.return_value = OTAStatus.FAILURE
        _boot_controller.get_booted_failure_info.return_value = (
            FailureType.RECOVERABLE,
            "E504: stalled",
        )
        mocker.patch(
            f"{OTA_CORE_MAIN_MODULE}.get_boot_controller",
            return_value=lambda: _boot_controller,
        )
        _put_spy = mocker.spy(status_report_queue, "put_nowait")

        OTAClient(
            ecu_status_flags=mocker.MagicMock(),
            status_report_queue=status_report_queue,
            client_update_control_flags=mocker.MagicMock(),
            shm_metrics_reader=mocker.MagicMock(),
        )

        _status_change_reports = [
            _call.args[0].payload
            for _call in _put_spy.call_args_list
            if isinstance(_call.args[0].payload, OTAStatusChangeReport)
        ]
        assert _status_change_reports == [
            OTAStatusChangeReport(
                new_ota_status=OTAStatus.FAILURE,
                failure_type=FailureType.RECOVERABLE,
                failure_reason="E504: stalled",
            )
        ]

    def test_update_upper_otaproxy_unreachable(self, mocker: pytest_mock.MockerFixture):
        mocker.patch.object(type(self.ota_client._metrics), "publish")
        mocker.patch.object(self.ota_client, "_exit_from_dynamic_client")
        mocker.patch(
            f"{OTA_CORE_COMMON_MODULE}.ensure_otaproxy_start",
            side_effect=ConnectionError("timeout"),
        )
        _on_failure_spy = mocker.spy(self.ota_client, "_on_failure")
        self.ota_client.proxy = "http://10.0.0.1:8082"

        self.ota_client.update(
            request=UpdateRequestV2(
                version=self.UPDATE_FIRMWARE_VERSION,
                url_base=self.OTA_IMAGE_URL,
                cookies_json=self.UPDATE_COOKIES_JSON,
                request_id="test-request-id",
                session_id="test_update_upper_otaproxy_unreachable",
            )
        )

        self.ota_updater.execute.assert_not_called()
        assert self.ota_client.live_ota_status == OTAStatus.FAILURE
        _on_failure_spy.assert_called_once()
        _failure_reason = _on_failure_spy.call_args.kwargs["failure_reason"]
        assert _failure_reason.startswith("E523: ")
        assert "http://10.0.0.1:8082" in _failure_reason

    def test_update_unexpected_exception(self, mocker: pytest_mock.MockerFixture):
        mocker.patch.object(type(self.ota_client._metrics), "publish")
        mocker.patch.object(self.ota_client, "_exit_from_dynamic_client")
        self.ota_updater.execute.side_effect = RuntimeError("unexpected by test")
        _on_failure_spy = mocker.spy(self.ota_client, "_on_failure")

        self.ota_client.update(
            request=UpdateRequestV2(
                version=self.UPDATE_FIRMWARE_VERSION,
                url_base=self.OTA_IMAGE_URL,
                cookies_json=self.UPDATE_COOKIES_JSON,
                request_id="test-request-id",
                session_id="test_update_unexpected_exception",
            )
        )

        assert self.ota_client.live_ota_status == OTAStatus.FAILURE
        _on_failure_spy.assert_called_once()
        _failure_reason = _on_failure_spy.call_args.kwargs["failure_reason"]
        assert _failure_reason.startswith("E500: ")
        assert "unexpected by test" in _failure_reason

    def test_update_invalid_cookies(self, mocker: pytest_mock.MockerFixture):
        mocker.patch.object(type(self.ota_client._metrics), "publish")
        mocker.patch.object(self.ota_client, "_exit_from_dynamic_client")
        _on_failure_spy = mocker.spy(self.ota_client, "_on_failure")

        self.ota_client.update(
            request=UpdateRequestV2(
                version=self.UPDATE_FIRMWARE_VERSION,
                url_base=self.OTA_IMAGE_URL,
                cookies_json="not-a-json",
                request_id="test-request-id",
                session_id="test_update_invalid_cookies",
            )
        )

        self.ota_updater.execute.assert_not_called()
        assert self.ota_client.live_ota_status == OTAStatus.FAILURE
        _on_failure_spy.assert_called_once()
        _failure_reason = _on_failure_spy.call_args.kwargs["failure_reason"]
        assert _failure_reason.startswith("E400: ")
        assert "not-a-json" not in _failure_reason

    def test_client_update_normal_finished(self):
        """Test client update with normal completion."""
        from otaclient._types import ClientUpdateRequestV2

        self.ota_client.client_update(
            request=ClientUpdateRequestV2(
                version=self.UPDATE_FIRMWARE_VERSION,
                url_base=self.OTA_IMAGE_URL,
                cookies_json=self.UPDATE_COOKIES_JSON,
                request_id="test-request-id",
                session_id="test_client_update_normal_finished",
            )
        )

        self.ota_client_updater.execute.assert_called_once()
        assert self.ota_client.live_ota_status == OTAStatus.CLIENT_UPDATING

    def test_client_update_interrupted(self, mocker: pytest_mock.MockerFixture):
        """Test client update with interruption."""
        from otaclient._types import ClientUpdateRequestV2

        _error = OTAErrorRecoverable(
            "client update interrupted by test as expected", module=__name__
        )
        self.ota_client_updater.execute.side_effect = _error
        self.ota_client._client_update_control_flags.request_shutdown_event.set = (
            mocker.MagicMock()
        )

        self.ota_client.client_update(
            request=ClientUpdateRequestV2(
                version=self.UPDATE_FIRMWARE_VERSION,
                url_base=self.OTA_IMAGE_URL,
                cookies_json=self.UPDATE_COOKIES_JSON,
                request_id="test-request-id",
                session_id="test_client_update_interrupted",
            )
        )

        self.ota_client_updater.execute.assert_called_once()
        assert self.ota_client.live_ota_status == OTAStatus.SUCCESS
