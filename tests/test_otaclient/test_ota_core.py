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

import shutil
from pathlib import Path
from queue import Queue

import pytest
import pytest_mock

from ota_metadata.utils.cert_store import load_ca_cert_chains
from otaclient import ota_core
from otaclient._status_monitor import (
    OTAClientStatusCollector,
    OTAStatusChangeReport,
    StatusReport,
)
from otaclient._types import OTAStatus, UpdateRequestV2
from otaclient.boot_control import BootControllerProtocol
from otaclient.configs.cfg import cfg as otaclient_cfg
from otaclient.create_standby.rebuild_mode import RebuildMode
from otaclient.errors import OTAErrorRecoverable
from otaclient.metrics import OTAMetricsData
from otaclient.ota_core import OTAClient, _OTAClientUpdater, _OTAUpdater
from tests.conftest import TestConfiguration as cfg
from tests.utils import SlotMeta

OTA_CORE_MODULE = ota_core.__name__


@pytest.fixture(autouse=True, scope="module")
def mock_certs_dir(module_mocker: pytest_mock.MockerFixture):
    """Mock to use the certs from the OTA test base image."""
    from otaclient.ota_core import cfg as _cfg

    module_mocker.patch.object(
        _cfg,
        "CERT_DPATH",
        cfg.CERTS_DIR,
    )


class TestOTAUpdater:
    """
    NOTE: the boot_control is mocked.
    """

    SESSION_ID = "session_id_for_test"

    @pytest.fixture
    def prepare_ab_slots(self, tmp_path: Path, ab_slots: SlotMeta):
        self.slot_a = Path(ab_slots.slot_a)
        self.slot_b = Path(ab_slots.slot_b)
        self.slot_a_boot_dir = Path(ab_slots.slot_a_boot_dev) / "boot"
        self.slot_b_boot_dir = Path(ab_slots.slot_b_boot_dev) / "boot"
        self.ota_image_dir = Path(cfg.OTA_IMAGE_DIR)

        self.otaclient_run_dir = tmp_path / "otaclient_run_dir"
        self.otaclient_run_dir.mkdir(parents=True, exist_ok=True)

        # ------ cleanup and prepare slot_b ------ #
        shutil.rmtree(self.slot_b, ignore_errors=True)
        self.slot_b.mkdir(exist_ok=True)
        # some important paths
        self.ota_metafiles_tmp_dir = self.slot_b / Path(
            otaclient_cfg.OTA_TMP_META_STORE
        ).relative_to("/")
        self.ota_tmp_dir = self.slot_b / Path(otaclient_cfg.OTA_TMP_STORE).relative_to(
            "/"
        )

        yield
        # cleanup slot_b after test
        shutil.rmtree(self.slot_b, ignore_errors=True)

    @pytest.fixture(autouse=True)
    def mock_setup(self, mocker: pytest_mock.MockerFixture, prepare_ab_slots):
        # ------ mock boot_controller ------ #
        self._boot_control = _boot_control_mock = mocker.MagicMock(
            spec=BootControllerProtocol
        )
        _boot_control_mock.get_standby_slot_path.return_value = self.slot_b

        # ------ mock otaclient cfg ------ #
        mocker.patch(f"{OTA_CORE_MODULE}.cfg.ACTIVE_SLOT_MNT", str(self.slot_a))
        mocker.patch(f"{OTA_CORE_MODULE}.cfg.STANDBY_SLOT_MNT", str(self.slot_b))
        mocker.patch(f"{OTA_CORE_MODULE}.cfg.RUN_DIR", str(self.otaclient_run_dir))

    def test_otaupdater(
        self,
        ota_status_collector: tuple[OTAClientStatusCollector, Queue[StatusReport]],
        mocker: pytest_mock.MockerFixture,
    ) -> None:
        _, report_queue = ota_status_collector
        ecu_status_flags = mocker.MagicMock()
        ecu_status_flags.any_child_ecu_in_update.is_set = mocker.MagicMock(
            return_value=False
        )

        # ------ execution ------ #
        ca_store = load_ca_cert_chains(cfg.CERTS_DIR)

        # update OTA status to update and assign session_id before execution
        report_queue.put_nowait(
            StatusReport(
                payload=OTAStatusChangeReport(
                    new_ota_status=OTAStatus.UPDATING,
                ),
                session_id=self.SESSION_ID,
            )
        )

        _updater = _OTAUpdater(
            version=cfg.UPDATE_VERSION,
            raw_url_base=cfg.OTA_IMAGE_URL,
            cookies_json=r'{"test": "my-cookie"}',
            ca_chains_store=ca_store,
            boot_controller=self._boot_control,
            upper_otaproxy=None,
            create_standby_cls=RebuildMode,
            ecu_status_flags=ecu_status_flags,
            session_id=self.SESSION_ID,
            status_report_queue=report_queue,
            metrics=OTAMetricsData(),
        )
        _updater._process_persistents = process_persists_handler = mocker.MagicMock()

        _updater.execute()

        # ------ assertions ------ #
        # assert the control_flags has been waited
        ecu_status_flags.any_child_ecu_in_update.is_set.assert_called_once()

        assert _updater.update_version == str(cfg.UPDATE_VERSION)

        self._boot_control.pre_update.assert_called_once()
        self._boot_control.post_update.assert_called_once()
        process_persists_handler.assert_called_once()


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

        # --- mock setup --- #
        self.control_flags = ecu_status_flags
        self.ota_updater = mocker.MagicMock(spec=_OTAUpdater)
        self.ota_client_updater = mocker.MagicMock(spec=_OTAClientUpdater)

        self.boot_controller = mocker.MagicMock(spec=BootControllerProtocol)

        # patch boot_controller for otaclient initializing
        self.boot_controller.load_version.return_value = self.CURRENT_FIRMWARE_VERSION
        self.boot_controller.get_booted_ota_status = mocker.MagicMock(
            return_value=OTAStatus.SUCCESS
        )

        # patch inject mocked updater
        mocker.patch(f"{OTA_CORE_MODULE}._OTAUpdater", return_value=self.ota_updater)
        mocker.patch(
            f"{OTA_CORE_MODULE}._OTAClientUpdater", return_value=self.ota_client_updater
        )
        mocker.patch(
            f"{OTA_CORE_MODULE}.get_boot_controller", return_value=self.boot_controller
        )

        # start otaclient
        self.ota_client = OTAClient(
            ecu_status_flags=ecu_status_flags,
            status_report_queue=status_report_queue,
            client_update_control_flags=client_update_control_flags,
        )

    def test_update_normal_finished(self):
        # --- execution --- #
        self.ota_client.update(
            request=UpdateRequestV2(
                version=self.UPDATE_FIRMWARE_VERSION,
                url_base=self.OTA_IMAGE_URL,
                cookies_json=self.UPDATE_COOKIES_JSON,
                session_id="test_update_normal_finished",
            )
        )

        # --- assert on update finished(before reboot) --- #
        self.ota_updater.execute.assert_called_once()
        assert self.ota_client.live_ota_status == OTAStatus.UPDATING

    def test_update_interrupted(self):
        # inject exception
        _error = OTAErrorRecoverable("interrupted by test as expected", module=__name__)
        self.ota_updater.execute.side_effect = _error

        # --- execution --- #
        self.ota_client.update(
            request=UpdateRequestV2(
                version=self.UPDATE_FIRMWARE_VERSION,
                url_base=self.OTA_IMAGE_URL,
                cookies_json=self.UPDATE_COOKIES_JSON,
                session_id="test_updaste_interrupted",
            )
        )

        # --- assertion on interrupted OTA update --- #
        self.ota_updater.execute.assert_called_once()
        assert self.ota_client.live_ota_status == OTAStatus.FAILURE

    def test_client_update_normal_finished(self):
        """Test client update with normal completion."""
        from otaclient._types import ClientUpdateRequestV2

        # --- execution --- #
        self.ota_client.client_update(
            request=ClientUpdateRequestV2(
                version=self.UPDATE_FIRMWARE_VERSION,
                url_base=self.OTA_IMAGE_URL,
                cookies_json=self.UPDATE_COOKIES_JSON,
                session_id="test_client_update_normal_finished",
            )
        )

        # --- assert on update finished --- #
        self.ota_client_updater.execute.assert_called_once()
        assert self.ota_client.live_ota_status == OTAStatus.CLIENT_UPDATING

    def test_client_update_interrupted(self):
        """Test client update with interruption."""
        from otaclient._types import ClientUpdateRequestV2

        # inject exception
        _error = OTAErrorRecoverable(
            "client update interrupted by test as expected", module=__name__
        )
        self.ota_client_updater.execute.side_effect = _error

        # --- execution --- #
        self.ota_client.client_update(
            request=ClientUpdateRequestV2(
                version=self.UPDATE_FIRMWARE_VERSION,
                url_base=self.OTA_IMAGE_URL,
                cookies_json=self.UPDATE_COOKIES_JSON,
                session_id="test_client_update_interrupted",
            )
        )

        # --- assertion on interrupted client update --- #
        self.ota_client_updater.execute.assert_called_once()
        assert self.ota_client.live_ota_status == OTAStatus.FAILURE


class TestOTAClientUpdater:
    """Testing on OTAClientUpdater workflow."""

    SESSION_ID = "client_update_session_id"
    CLIENT_UPDATE_VERSION = "client_update_version"
    CLIENT_UPDATE_URL = "https://example.com/ota-client/"
    CLIENT_UPDATE_COOKIES_JSON = r'{"test": "client-cookie"}'

    @pytest.fixture(autouse=True)
    def mock_setup(
        self,
        ota_status_collector: tuple[OTAClientStatusCollector, Queue[StatusReport]],
        mocker: pytest_mock.MockerFixture,
        tmp_path: Path,
    ):
        _, self.status_report_queue = ota_status_collector
        self.ecu_status_flags = mocker.MagicMock()
        self.client_update_control_flags = mocker.MagicMock()

        # Create a real temporary directory for the session
        self.session_workdir = tmp_path / "test_client_update"
        self.session_workdir.mkdir(parents=True, exist_ok=True)

        # Mock OTAClientPackageDownloader
        self.mock_ota_client_package = mocker.MagicMock()
        self.patcher_client_package = mocker.patch(
            f"{OTA_CORE_MODULE}.OTAClientPackageDownloader",
            return_value=self.mock_ota_client_package,
        )

        # Mock DownloaderPool
        self.mock_downloader_pool = mocker.MagicMock()
        self.patcher_downloader_pool = mocker.patch(
            f"{OTA_CORE_MODULE}.DownloaderPool", return_value=self.mock_downloader_pool
        )

        # Set up CA chains store
        self.ca_chains_store = load_ca_cert_chains(cfg.CERTS_DIR)

        # Mock the creation of session directory
        mocker.patch(f"{OTA_CORE_MODULE}.Path.mkdir")

        # Mock shutil.rmtree
        self.rmtree_patcher = mocker.patch("shutil.rmtree")

    def setup_client_updater(self, mocker: pytest_mock.MockerFixture):
        # Create an instance of _OTAClientUpdater for testing
        client_updater = ota_core._OTAClientUpdater(
            version=self.CLIENT_UPDATE_VERSION,
            raw_url_base=self.CLIENT_UPDATE_URL,
            cookies_json=self.CLIENT_UPDATE_COOKIES_JSON,
            ca_chains_store=self.ca_chains_store,
            ecu_status_flags=self.ecu_status_flags,
            status_report_queue=self.status_report_queue,
            session_id=self.SESSION_ID,
            client_update_control_flags=self.client_update_control_flags,
            upper_otaproxy=None,
            metrics=OTAMetricsData(),
        )

        # Patch the _session_workdir attribute after instance creation
        mocker.patch.object(client_updater, "_session_workdir", self.session_workdir)

        return client_updater

    def test_client_updater_init(self, mocker: pytest_mock.MockerFixture):
        # Test initialization of _OTAClientUpdater

        # Instead of trying to mock the parent class init, create the instance directly
        client_updater = self.setup_client_updater(mocker)

        # Assert initialization parameters
        assert (
            client_updater.client_update_control_flags.stop_server_event
            == self.client_update_control_flags.stop_server_event
        )
        assert (
            client_updater.client_update_control_flags.request_shutdown_event
            == self.client_update_control_flags.request_shutdown_event
        )
        assert client_updater.update_version == self.CLIENT_UPDATE_VERSION

    def test_download_client_package_resources(self, mocker: pytest_mock.MockerFixture):
        # Test downloading client package resources
        client_updater = self.setup_client_updater(mocker)

        # Mock the internal _download_client_package_files method
        mock_download_files = mocker.patch.object(
            client_updater, "_download_client_package_files"
        )

        # Mock the status report queue's put_nowait method
        mock_put_nowait = mocker.patch.object(self.status_report_queue, "put_nowait")

        client_updater._download_client_package_resources()

        # Verify correct status update was sent
        mock_put_nowait.assert_called_once()
        call_args = mock_put_nowait.call_args
        status_report = call_args[0][0]  # Get first positional argument

        assert (
            status_report.payload.new_update_phase
            == ota_core.UpdatePhase.DOWNLOADING_OTA_CLIENT
        )

        # Verify download method was called
        mock_download_files.assert_called_once()

    def test_download_client_package_files(self, mocker: pytest_mock.MockerFixture):
        # Test downloading client package files
        client_updater = self.setup_client_updater(mocker)

        # Mock _download_and_process_file_with_condition method
        mock_download_method = mocker.patch.object(
            client_updater, "_download_and_process_file_with_condition"
        )

        client_updater._download_client_package_files()

        # Verify correct parameters were passed
        mock_download_method.assert_called_once_with(
            thread_name_prefix="download_client_file",
            get_downloads_generator=self.mock_ota_client_package.download_client_package,
        )

    def test_wait_sub_ecus(self, mocker: pytest_mock.MockerFixture):
        # Test waiting for sub-ECUs to complete
        client_updater = self.setup_client_updater(mocker)
        # Mock the wait_and_log function
        mock_wait_and_log = mocker.patch(f"{OTA_CORE_MODULE}.wait_and_log")
        # Case 1: Test successful waiting (wait_and_log returns True)
        mock_wait_and_log.return_value = True
        client_updater._wait_sub_ecus()
        # Verify that request_shutdown_event was not called (successful case)
        self.client_update_control_flags.request_shutdown_event.set.assert_not_called()

        # Reset mocks for second test case
        mock_wait_and_log.reset_mock()
        self.client_update_control_flags.request_shutdown_event.set.reset_mock()
        # Case 2: Test timeout or failure case (wait_and_log returns False)
        mock_wait_and_log.return_value = False
        client_updater._wait_sub_ecus()
        # Verify wait_and_log was called
        mock_wait_and_log.assert_called_once()

    def test_execute_client_update_flow(self, mocker: pytest_mock.MockerFixture):
        """Test the full execution flow of _execute_client_update."""
        client_updater = self.setup_client_updater(mocker)

        # Mock all the component methods
        mock_handle_proxy = mocker.patch.object(client_updater, "_handle_upper_proxy")
        mock_process_metadata = mocker.patch.object(client_updater, "_process_metadata")
        mock_download_resources = mocker.patch.object(
            client_updater, "_download_client_package_resources"
        )
        mock_wait_sub_ecus = mocker.patch.object(client_updater, "_wait_sub_ecus")
        mock_is_same_version = mocker.patch.object(
            client_updater, "_is_same_client_package_version", return_value=False
        )
        mock_copy_client_package = mocker.patch.object(
            client_updater, "_copy_client_package"
        )
        mock_notify_data_ready = mocker.patch.object(
            client_updater, "_notify_data_ready"
        )
        # Execute the client update
        client_updater._execute_client_update()

        # Verify the flow of method calls
        mock_handle_proxy.assert_called_once()
        mock_process_metadata.assert_called_once()
        mock_download_resources.assert_called_once()
        mock_wait_sub_ecus.assert_called_once()
        mock_is_same_version.assert_called_once()
        mock_copy_client_package.assert_called_once()
        mock_notify_data_ready.assert_called_once()

    def test_execute_client_update_same_version(
        self, mocker: pytest_mock.MockerFixture
    ):
        """Test the case where the client package version is the same."""
        client_updater = self.setup_client_updater(mocker)

        # Mock methods
        mocker.patch.object(client_updater, "_handle_upper_proxy")
        mocker.patch.object(client_updater, "_process_metadata")
        mocker.patch.object(client_updater, "_download_client_package_resources")
        mocker.patch.object(client_updater, "_wait_sub_ecus")
        mock_is_same_version = mocker.patch.object(
            client_updater, "_is_same_client_package_version", return_value=True
        )
        mock_notify_data_ready = mocker.patch.object(
            client_updater, "_notify_data_ready"
        )
        mock_request_shutdown = mocker.patch.object(client_updater, "_request_shutdown")

        # Execute the client update
        client_updater._execute_client_update()

        # Verify the flow of method calls
        mock_is_same_version.assert_called_once()
        mock_notify_data_ready.assert_not_called()  # Should not be called for the same version
        mock_request_shutdown.assert_called_once()

    def test_execute_client_update_failure(self, mocker: pytest_mock.MockerFixture):
        """Test the case where an exception occurs during the update process."""
        client_updater = self.setup_client_updater(mocker)

        # Mock methods to raise an exception
        mocker.patch.object(client_updater, "_handle_upper_proxy")
        mocker.patch.object(client_updater, "_process_metadata")
        mocker.patch.object(client_updater, "_download_client_package_resources")
        mocker.patch.object(client_updater, "_wait_sub_ecus")
        mocker.patch.object(
            client_updater, "_is_same_client_package_version", return_value=False
        )
        mock_copy_client_package = mocker.patch.object(
            client_updater,
            "_copy_client_package",
            side_effect=Exception("Test exception"),
        )
        mock_request_shutdown = mocker.patch.object(
            client_updater, "_request_shutdown", return_value=False
        )

        # Execute the client update
        client_updater._execute_client_update()

        # Verify the flow of method calls
        mock_copy_client_package.assert_called_once()
        mock_request_shutdown.assert_called_once()

    def test_execute_success(self, mocker: pytest_mock.MockerFixture):
        # Test successful execution
        client_updater = self.setup_client_updater(mocker)

        # Mock the main execution method
        mock_execute = mocker.patch.object(client_updater, "_execute_client_update")

        client_updater.execute()

        # Verify execution was successful
        mock_execute.assert_called_once()
        self.rmtree_patcher.assert_called_with(self.session_workdir, ignore_errors=True)

    def test_execute_failure(self, mocker: pytest_mock.MockerFixture):
        # Test execution with failure
        client_updater = self.setup_client_updater(mocker)

        # Mock execution to raise an exception
        mock_execute = mocker.patch.object(
            client_updater,
            "_execute_client_update",
            side_effect=ota_core.ota_errors.OTAError("Test error", module="test"),
        )

        # Exception should be raised through execute()
        with pytest.raises(ota_core.ota_errors.OTAError):
            client_updater.execute()

        # Verify error handling
        mock_execute.assert_called_once()
        self.rmtree_patcher.assert_called_with(self.session_workdir, ignore_errors=True)

    def test_download_with_tasks_ensure_failed(self, mocker: pytest_mock.MockerFixture):
        # Test handling of TasksEnsureFailed during download
        client_updater = self.setup_client_updater(mocker)

        # Import the exception class
        from otaclient_common.retry_task_map import TasksEnsureFailed

        # Mock _download_client_package_files to raise TasksEnsureFailed
        mocker.patch.object(
            client_updater,
            "_download_client_package_files",
            side_effect=TasksEnsureFailed(),
        )

        # Should raise NetworkError
        with pytest.raises(ota_core.ota_errors.NetworkError):
            client_updater._download_client_package_resources()

        # Verify downloader pool was shut down
        self.mock_downloader_pool.shutdown.assert_called_once()
