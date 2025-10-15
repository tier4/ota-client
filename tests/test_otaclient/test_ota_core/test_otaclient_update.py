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

from pathlib import Path
from queue import Queue

import pytest
import pytest_mock

from ota_metadata.utils.cert_store import load_ca_cert_chains
from otaclient import errors as ota_errors
from otaclient import ota_core
from otaclient._status_monitor import (
    OTAClientStatusCollector,
    StatusReport,
)
from otaclient._types import UpdatePhase
from otaclient.errors import ClientUpdateSameVersions
from otaclient.metrics import OTAMetricsData
from otaclient.ota_core import OTAClientUpdater
from otaclient.ota_core._common import create_downloader_pool
from tests.conftest import TestConfiguration as cfg

OTA_UPDATER_BASE_MODULE = ota_core._updater_base.__name__
OTACLIENT_UPDATER_MODULE = ota_core._client_updater.__name__


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
            f"{OTACLIENT_UPDATER_MODULE}.OTAClientPackageDownloader",
            return_value=self.mock_ota_client_package,
        )

        # Set up CA chains store
        self.ca_chains_store = load_ca_cert_chains(cfg.CERTS_DIR)
        self.downloader_pool = create_downloader_pool(
            raw_cookies_json=cfg.COOKIES_JSON,
            download_threads=3,
            chunk_size=1024**2,
        )

        # Mock the creation of session directory
        mocker.patch(f"{OTA_UPDATER_BASE_MODULE}.Path.mkdir")

        # Mock shutil.rmtree
        self.rmtree_patcher = mocker.patch("shutil.rmtree")

    @pytest.fixture
    def setup_client_updater(
        self, mocker: pytest_mock.MockerFixture, tmp_path: Path, mock_setup
    ):
        # Create an instance of OTAClientUpdater for testing
        session_workdir = tmp_path / "session_workdir"
        client_updater = ota_core.OTAClientUpdater(
            version=self.CLIENT_UPDATE_VERSION,
            raw_url_base=self.CLIENT_UPDATE_URL,
            session_wd=session_workdir,
            ca_chains_store=self.ca_chains_store,
            downloader_pool=self.downloader_pool,
            ecu_status_flags=self.ecu_status_flags,
            status_report_queue=self.status_report_queue,
            session_id=self.SESSION_ID,
            client_update_control_flags=self.client_update_control_flags,
            metrics=OTAMetricsData(),
            shm_metrics_reader=None,  # type: ignore
        )

        # Patch the _session_workdir attribute after instance creation
        mocker.patch.object(client_updater, "_session_workdir", self.session_workdir)

        return client_updater

    def test_client_updater_init(
        self, setup_client_updater, mocker: pytest_mock.MockerFixture
    ):
        # Test initialization of OTAClientUpdater

        # Instead of trying to mock the parent class init, create the instance directly
        client_updater = setup_client_updater

        # Assert initialization parameters
        assert (
            client_updater.client_update_control_flags.request_shutdown_event
            == self.client_update_control_flags.request_shutdown_event
        )
        assert client_updater.update_version == self.CLIENT_UPDATE_VERSION

    def test_download_client_package_resources(
        self, setup_client_updater: OTAClientUpdater, mocker: pytest_mock.MockerFixture
    ):
        # Test downloading client package resources
        client_updater = setup_client_updater

        # Mock the internal _download_client_package_files method
        mock_download_files = mocker.patch.object(
            client_updater,
            "_download_helper",
        )

        # Mock the status report queue's put_nowait method
        mock_put_nowait = mocker.patch.object(self.status_report_queue, "put_nowait")

        client_updater._download_client_package_resources()

        # Verify correct status update was sent
        mock_put_nowait.assert_called_once()
        call_args = mock_put_nowait.call_args
        status_report = call_args[0][0]  # Get first positional argument

        assert (
            status_report.payload.new_update_phase == UpdatePhase.DOWNLOADING_OTA_CLIENT
        )

        # Verify download method was called
        mock_download_files.download_meta_files.assert_called_once()

    def test_wait_sub_ecus(
        self, setup_client_updater, mocker: pytest_mock.MockerFixture
    ):
        # Test waiting for sub-ECUs to complete
        client_updater = setup_client_updater
        # Mock the wait_and_log function
        mock_wait_and_log = mocker.patch(f"{OTACLIENT_UPDATER_MODULE}.wait_and_log")
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

    def test_execute_client_update_flow(
        self, setup_client_updater, mocker: pytest_mock.MockerFixture
    ):
        """Test the full execution flow of _execute_client_update."""
        client_updater = setup_client_updater

        # Mock all the component methods
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
        mock_process_metadata.assert_called_once()
        mock_download_resources.assert_called_once()
        mock_wait_sub_ecus.assert_called_once()
        mock_is_same_version.assert_called_once()
        mock_copy_client_package.assert_called_once()
        mock_notify_data_ready.assert_called_once()

    def test_execute_client_update_same_version(
        self, setup_client_updater, mocker: pytest_mock.MockerFixture
    ):
        """Test the case where the client package version is the same."""
        client_updater = setup_client_updater

        # Mock methods
        mocker.patch.object(client_updater, "_process_metadata")
        mocker.patch.object(client_updater, "_download_client_package_resources")
        mocker.patch.object(client_updater, "_wait_sub_ecus")
        mock_is_same_version = mocker.patch.object(
            client_updater, "_is_same_client_package_version", return_value=True
        )
        mock_notify_data_ready = mocker.patch.object(
            client_updater, "_notify_data_ready"
        )

        # Execute the client update and expect ClientUpdateSameVersions exception
        with pytest.raises(ClientUpdateSameVersions):
            client_updater._execute_client_update()

        # Verify the flow of method calls
        mock_is_same_version.assert_called_once()
        mock_notify_data_ready.assert_not_called()  # Should not be called for the same version

    def test_execute_client_update_failure(
        self, setup_client_updater, mocker: pytest_mock.MockerFixture
    ):
        """Test the case where an exception occurs during the update process."""
        client_updater = setup_client_updater

        # Mock methods to raise an exception
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

        # Execute the client update
        with pytest.raises(Exception, match="Test exception"):
            client_updater._execute_client_update()

        # Verify the flow of method calls
        mock_copy_client_package.assert_called_once()

    def test_execute_success(
        self, setup_client_updater, mocker: pytest_mock.MockerFixture
    ):
        # Test successful execution
        client_updater = setup_client_updater

        # Mock the main execution method
        mock_execute = mocker.patch.object(client_updater, "_execute_client_update")

        client_updater.execute()

        # Verify execution was successful
        mock_execute.assert_called_once()
        self.rmtree_patcher.assert_called_with(self.session_workdir, ignore_errors=True)

    def test_execute_failure(
        self, setup_client_updater, mocker: pytest_mock.MockerFixture
    ):
        # Test execution with failure
        client_updater = setup_client_updater

        # Mock execution to raise an exception
        mock_execute = mocker.patch.object(
            client_updater,
            "_execute_client_update",
            side_effect=ota_errors.OTAError("Test error", module="test"),
        )

        # Exception should be raised through execute()
        with pytest.raises(ota_errors.OTAError):
            client_updater.execute()

        # Verify error handling
        mock_execute.assert_called_once()
        self.rmtree_patcher.assert_called_with(self.session_workdir, ignore_errors=True)

    def test_download_with_tasks_ensure_failed(
        self, setup_client_updater: OTAClientUpdater, mocker: pytest_mock.MockerFixture
    ):
        # Test handling of TasksEnsureFailed during download
        client_updater = setup_client_updater

        # Import the exception class
        from otaclient_common.retry_task_map import TasksEnsureFailed

        mock_downloader_helper = mocker.MagicMock()
        mock_downloader_helper.download_meta_files = mocker.MagicMock(
            side_effect=TasksEnsureFailed
        )
        mock_downloader_pool = mocker.MagicMock()

        # Mock _download_client_package_resources to raise TasksEnsureFailed
        mocker.patch.object(client_updater, "_download_helper", mock_downloader_helper)
        mocker.patch.object(client_updater, "_downloader_pool", mock_downloader_pool)

        with pytest.raises(ota_errors.OTAClientPackageDownloadFailed):
            client_updater._download_client_package_resources()

        # Verify downloader pool was shut down
        mock_downloader_pool.shutdown.assert_called_once()
