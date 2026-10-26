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

import time
from pathlib import Path
from queue import Empty, Queue
from typing import Protocol

import pytest
import pytest_mock

from otaclient import ota_core
from otaclient._status_monitor import (
    OTAClientStatusCollector,
    OTAStatusChangeReport,
    StatusReport,
)
from otaclient._types import (
    ClientUpdateRequestV2,
    FailureType,
    IPCResEnum,
    OTAStatus,
    UpdateRequestV2,
)
from otaclient.boot_control import BootControllerProtocol
from otaclient.boot_control.protocol import PartitionLayoutBootControllerProtocol
from otaclient.errors import OTAErrorRecoverable
from otaclient.ota_core import (
    OTAClient,
    OTAClientUpdater,
    OTAUpdaterForLegacyOTAImage,
    OTAUpdaterForOTAImageV1,
)
from otaclient.ota_core._partition_image import OTAUpdaterForPartitionImage

OTA_CORE_MAIN_MODULE = ota_core._main.__name__
OTA_CORE_COMMON_MODULE = ota_core._common.__name__


class _StopMainLoop(Exception):
    """Raised from a mocked request queue to leave OTAClient.main()."""


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
        # NOTE: the instance OTAClient builds from it is spec'd like the real
        #       controllers: on Python < 3.12 a runtime_checkable Protocol is checked
        #       with hasattr, which an unspec'd MagicMock answers for any name, and
        #       would make every test run on a partition layout.
        _instance = mocker.MagicMock(spec=BootControllerProtocol)
        _instance.load_version.return_value = self.CURRENT_FIRMWARE_VERSION
        _instance.get_booted_ota_status = mocker.MagicMock(
            return_value=OTAStatus.SUCCESS
        )
        self.boot_controller.return_value = _instance

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

    def test_update_on_a_partition_layout_goes_through_its_installer(
        self, mocker: pytest_mock.MockerFixture
    ):
        """A boot controller of a partition-based layout provides the installer;
        a version 1 image is then applied through it, not file by file."""

        class _PartitionLayoutBootController(
            BootControllerProtocol, PartitionLayoutBootControllerProtocol, Protocol
        ): ...

        _boot_controller = mocker.MagicMock(spec=_PartitionLayoutBootController)
        _boot_controller.payload_staging_dir = Path("/opt/data/otaclient/staging")
        self.ota_client.boot_controller = _boot_controller
        self.ota_client.ca_store = mocker.MagicMock()
        mocker.patch(f"{OTA_CORE_MAIN_MODULE}.check_if_ota_image_v1", return_value=True)
        _file_based_updater = mocker.patch(
            f"{OTA_CORE_MAIN_MODULE}.OTAUpdaterForOTAImageV1"
        )
        _partition_updater = mocker.MagicMock(spec=OTAUpdaterForPartitionImage)
        _partition_updater_cls = mocker.patch(
            f"{OTA_CORE_MAIN_MODULE}.OTAUpdaterForPartitionImage",
            return_value=_partition_updater,
        )
        mocker.patch.object(type(self.ota_client._metrics), "publish")

        self.ota_client.update(
            request=UpdateRequestV2(
                version=self.UPDATE_FIRMWARE_VERSION,
                url_base=self.OTA_IMAGE_URL,
                cookies_json=self.UPDATE_COOKIES_JSON,
                request_id="test-request-id",
                session_id="test_update_on_a_partition_layout",
            )
        )

        _partition_updater.execute.assert_called_once()
        _file_based_updater.assert_not_called()
        _kwargs = _partition_updater_cls.call_args.kwargs
        assert _kwargs["installer"] is _boot_controller.partition_installer
        assert _kwargs["staging_dir"] == Path("/opt/data/otaclient/staging")
        assert _kwargs["boot_controller"] is _boot_controller

    def test_update_with_a_legacy_image_on_a_partition_layout_is_refused(
        self, mocker: pytest_mock.MockerFixture
    ):
        """A legacy image is applied file by file into a mounted standby slot, which
        a partition-based layout does not have: refused as an invalid image before
        any status record is written, not as an 'unspecific error' half-way."""

        class _PartitionLayoutBootController(
            BootControllerProtocol, PartitionLayoutBootControllerProtocol, Protocol
        ): ...

        _boot_controller = mocker.MagicMock(spec=_PartitionLayoutBootController)
        self.ota_client.boot_controller = _boot_controller
        self.ota_client.ca_chains_store = mocker.MagicMock()
        mocker.patch(
            f"{OTA_CORE_MAIN_MODULE}.check_if_ota_image_v1", return_value=False
        )
        _legacy_updater_cls = mocker.patch(
            f"{OTA_CORE_MAIN_MODULE}.OTAUpdaterForLegacyOTAImage"
        )
        mocker.patch.object(type(self.ota_client._metrics), "publish")

        self.ota_client.update(
            request=UpdateRequestV2(
                version=self.UPDATE_FIRMWARE_VERSION,
                url_base=self.OTA_IMAGE_URL,
                cookies_json=self.UPDATE_COOKIES_JSON,
                request_id="test-request-id",
                session_id="test_update_with_a_legacy_image_on_a_partition_layout",
            )
        )

        _legacy_updater_cls.assert_not_called()
        _boot_controller.pre_update.assert_not_called()
        assert self.ota_client.live_ota_status == OTAStatus.FAILURE
        _boot_controller.store_failure_info.assert_called_once()
        assert (
            "E422"
            in _boot_controller.store_failure_info.call_args.kwargs["failure_reason"]
        )

    def test_update_with_a_v1_image_on_an_ordinary_boot_controller_is_file_based(
        self, mocker: pytest_mock.MockerFixture
    ):
        """A boot controller that provides no partition installer (every one today)
        applies a version 1 image file by file, as before this updater existed."""
        # NOTE: a spec'd mock, as the real controllers: on Python < 3.12 a
        #       runtime_checkable Protocol is checked with hasattr, which an
        #       unspec'd MagicMock answers for any name.
        self.ota_client.boot_controller = mocker.MagicMock(spec=BootControllerProtocol)
        self.ota_client.ca_store = mocker.MagicMock()
        mocker.patch(f"{OTA_CORE_MAIN_MODULE}.check_if_ota_image_v1", return_value=True)
        _file_based_updater = mocker.MagicMock(spec=OTAUpdaterForOTAImageV1)
        _file_based_updater_cls = mocker.patch(
            f"{OTA_CORE_MAIN_MODULE}.OTAUpdaterForOTAImageV1",
            return_value=_file_based_updater,
        )
        _partition_updater_cls = mocker.patch(
            f"{OTA_CORE_MAIN_MODULE}.OTAUpdaterForPartitionImage"
        )
        mocker.patch.object(type(self.ota_client._metrics), "publish")

        self.ota_client.update(
            request=UpdateRequestV2(
                version=self.UPDATE_FIRMWARE_VERSION,
                url_base=self.OTA_IMAGE_URL,
                cookies_json=self.UPDATE_COOKIES_JSON,
                request_id="test-request-id",
                session_id="test_update_on_an_ordinary_boot_controller",
            )
        )

        _file_based_updater.execute.assert_called_once()
        _partition_updater_cls.assert_not_called()
        _kwargs = _file_based_updater_cls.call_args.kwargs
        assert _kwargs["boot_controller"] is self.ota_client.boot_controller

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

    def _make_ota_client_booted_with(
        self,
        booted_ota_status: OTAStatus,
        status_report_queue: Queue[StatusReport],
        mocker: pytest_mock.MockerFixture,
    ) -> OTAClient:
        _boot_controller = mocker.MagicMock(spec=BootControllerProtocol)
        _boot_controller.load_version.return_value = self.CURRENT_FIRMWARE_VERSION
        _boot_controller.load_version_detail.return_value = None
        _boot_controller.get_booted_ota_status.return_value = booted_ota_status
        _boot_controller.get_booted_failure_info.return_value = None
        mocker.patch(
            f"{OTA_CORE_MAIN_MODULE}.get_boot_controller",
            return_value=lambda: _boot_controller,
        )
        return OTAClient(
            ecu_status_flags=mocker.MagicMock(),
            status_report_queue=status_report_queue,
            client_update_control_flags=mocker.MagicMock(),
            shm_metrics_reader=mocker.MagicMock(),
        )

    def test_aborted_status_reported_briefly_on_startup(
        self,
        ota_status_collector: tuple[OTAClientStatusCollector, Queue[StatusReport]],
        mocker: pytest_mock.MockerFixture,
    ):
        """ABORTED loaded at startup is reported for ABORTED_STATUS_REPORT_DURATION,
        then INITIALIZED is reported so that a main ECU running otaclient < v3.14.0
        can parse this ECU's status again."""
        _, status_report_queue = ota_status_collector

        # a startup with any other status does not open the ABORTED report window
        _normal = self._make_ota_client_booted_with(
            OTAStatus.SUCCESS, status_report_queue, mocker
        )
        assert _normal.live_ota_status == OTAStatus.SUCCESS
        assert _normal._aborted_status_report_deadline is None

        _put_spy = mocker.spy(status_report_queue, "put_nowait")
        ota_client = self._make_ota_client_booted_with(
            OTAStatus.ABORTED, status_report_queue, mocker
        )
        assert ota_client.live_ota_status == OTAStatus.ABORTED
        _deadline = ota_client._aborted_status_report_deadline
        assert _deadline is not None

        # within the report window ABORTED stays
        ota_client._expire_aborted_status_report(_deadline - 1)
        assert ota_client.live_ota_status == OTAStatus.ABORTED
        assert ota_client._aborted_status_report_deadline == _deadline

        # after the report window INITIALIZED is reported
        ota_client._expire_aborted_status_report(_deadline)
        assert ota_client.live_ota_status == OTAStatus.INITIALIZED
        assert ota_client._aborted_status_report_deadline is None

        # an expired window never overwrites a live status other than ABORTED
        ota_client._aborted_status_report_deadline = _deadline
        ota_client._live_ota_status = OTAStatus.UPDATING
        ota_client._expire_aborted_status_report(_deadline)
        assert ota_client.live_ota_status == OTAStatus.UPDATING
        assert ota_client._aborted_status_report_deadline is None

        _status_change_reports = [
            _call.args[0].payload
            for _call in _put_spy.call_args_list
            if isinstance(_call.args[0].payload, OTAStatusChangeReport)
        ]
        assert _status_change_reports == [
            OTAStatusChangeReport(new_ota_status=OTAStatus.ABORTED),
            OTAStatusChangeReport(new_ota_status=OTAStatus.INITIALIZED),
        ]

    def test_main_loop_expires_aborted_status_report(
        self, mocker: pytest_mock.MockerFixture
    ):
        """The request dispatching loop replaces ABORTED with INITIALIZED once the
        report window has passed, also when no request arrives."""
        self.ota_client._live_ota_status = OTAStatus.ABORTED
        self.ota_client._aborted_status_report_deadline = time.monotonic() - 1
        req_queue = mocker.MagicMock()
        req_queue.get.side_effect = [Empty(), _StopMainLoop()]

        with pytest.raises(_StopMainLoop):
            self.ota_client.main(req_queue=req_queue, resp_queue=mocker.MagicMock())

        assert self.ota_client.live_ota_status == OTAStatus.INITIALIZED
        assert self.ota_client._aborted_status_report_deadline is None

    @pytest.mark.parametrize(
        "request_cls, handler_name",
        [
            (UpdateRequestV2, "update"),
            (ClientUpdateRequestV2, "client_update"),
        ],
    )
    def test_main_loop_closes_aborted_status_report_window_on_request(
        self,
        request_cls: type,
        handler_name: str,
        mocker: pytest_mock.MockerFixture,
    ):
        """Accepting an update or a client update closes the ABORTED report window,
        so the loop never overwrites the status of the OTA that follows."""
        mocker.patch.object(self.ota_client, handler_name)
        self.ota_client._live_ota_status = OTAStatus.ABORTED
        self.ota_client._aborted_status_report_deadline = time.monotonic() + 300
        req_queue = mocker.MagicMock()
        req_queue.get.side_effect = [
            request_cls(
                version=self.UPDATE_FIRMWARE_VERSION,
                url_base=self.OTA_IMAGE_URL,
                cookies_json=self.UPDATE_COOKIES_JSON,
                request_id="test-request-id",
                session_id=f"test_window_closed_on_{handler_name}",
            ),
            _StopMainLoop(),
        ]
        resp_queue = mocker.MagicMock()

        with pytest.raises(_StopMainLoop):
            self.ota_client.main(req_queue=req_queue, resp_queue=resp_queue)

        assert resp_queue.put_nowait.call_args.args[0].res == IPCResEnum.ACCEPT
        assert self.ota_client._aborted_status_report_deadline is None

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
