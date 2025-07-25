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

import multiprocessing.shared_memory as mp_shm
import signal
from unittest.mock import MagicMock, patch

import pytest
import pytest_mock

from otaclient import main
from otaclient.configs.cfg import cfg

MAIN_MODULE = "otaclient.main"


class TestMain:
    """Tests for the main module of OTA client."""

    @pytest.fixture(autouse=True)
    def setup(self, mocker: pytest_mock.MockerFixture):
        # Reset global variables before each test
        main._ota_core_p = None
        main._grpc_server_p = None
        main._shm = None

        # Mock shared memory
        self.mock_shm = mocker.MagicMock(spec=mp_shm.SharedMemory)
        self.mock_shm.name = "test_shm_name"
        mocker.patch(f"{MAIN_MODULE}.mp_shm.SharedMemory", return_value=self.mock_shm)

        # Mock proxy_info to avoid validation errors
        # Instead of trying to mock it at module level, we'll mock it where it's imported
        mock_proxy_info = mocker.MagicMock()
        mock_proxy_info.enable_local_ota_proxy = False
        mock_proxy_info.local_ota_proxy = {}
        mocker.patch("otaclient.configs.cfg.proxy_info", mock_proxy_info)

        # Mock common functions to prevent side effects
        mocker.patch(f"{MAIN_MODULE}.secrets.token_bytes", return_value=b"mock_key")
        mocker.patch(f"{MAIN_MODULE}.time.sleep")
        mocker.patch(f"{MAIN_MODULE}.atexit.register")
        mocker.patch(f"{MAIN_MODULE}.signal.signal")

        # Mock the imported utility functions
        mocker.patch("otaclient._utils.check_other_otaclient")

        # Mock processes
        self.mock_ota_core_p = mocker.MagicMock()
        self.mock_grpc_server_p = mocker.MagicMock()

        # Return values for is_alive checks
        self.mock_ota_core_p.is_alive.return_value = True
        self.mock_grpc_server_p.is_alive.return_value = True

    def test_on_shutdown(self, mocker: pytest_mock.MockerFixture):
        """Test the _on_shutdown function."""
        sys_exit_mock = mocker.patch(f"{MAIN_MODULE}.sys.exit")

        # Set up global variables
        main._ota_core_p = self.mock_ota_core_p
        main._grpc_server_p = self.mock_grpc_server_p
        main._shm = self.mock_shm

        # Patch os.killpg and os.getpgid
        getpgid_mock = mocker.patch(f"{MAIN_MODULE}.os.getpgid", return_value=4321)

        # Call with sys_exit=False
        main._on_shutdown(sys_exit=False)

        # Verify processes are terminated and joined
        self.mock_ota_core_p.terminate.assert_called_once()
        self.mock_ota_core_p.join.assert_called_once()
        self.mock_grpc_server_p.terminate.assert_called_once()
        self.mock_grpc_server_p.join.assert_called_once()

        # Verify shm is closed and unlinked
        self.mock_shm.close.assert_called_once()
        self.mock_shm.unlink.assert_called_once()

        # Verify sys.exit not called
        sys_exit_mock.assert_not_called()

        # Reset mocks for next test
        self.mock_ota_core_p.reset_mock()
        self.mock_grpc_server_p.reset_mock()
        self.mock_shm.reset_mock()
        getpgid_mock.reset_mock()

        # Set up global variables again
        main._ota_core_p = self.mock_ota_core_p
        main._grpc_server_p = self.mock_grpc_server_p
        main._shm = self.mock_shm

        # Call with sys_exit=True
        main._on_shutdown(sys_exit=True)

        # Verify processes are terminated and joined
        self.mock_ota_core_p.terminate.assert_called_once()
        self.mock_ota_core_p.join.assert_called_once()
        self.mock_grpc_server_p.terminate.assert_called_once()
        self.mock_grpc_server_p.join.assert_called_once()

        # Verify shm is closed and unlinked
        self.mock_shm.close.assert_called_once()
        self.mock_shm.unlink.assert_called_once()

        # Verify sys.exit called with 1
        sys_exit_mock.assert_called_once_with(1)

    def test_signal_handler(self, mocker: pytest_mock.MockerFixture):
        """Test the _signal_handler function."""
        on_shutdown_mock = mocker.patch(f"{MAIN_MODULE}._on_shutdown")

        # Call the signal handler with SIGTERM
        main._signal_handler(signal.SIGTERM, None)

        # Verify _on_shutdown is called with sys_exit=True
        on_shutdown_mock.assert_called_once_with(sys_exit=True)

    @patch("otaclient._logging.configure_logging")
    def test_main_process_setup_and_health_check(
        self, mock_logging, mocker: pytest_mock.MockerFixture
    ):
        """Test the main function process setup and health check loop."""
        # Skip the full execution of main() by making time.sleep raise an exception after one call
        mocker.patch(
            f"{MAIN_MODULE}.time.sleep", side_effect=[None, Exception("Break loop")]
        )

        # Mock the modules and functions imported in main()
        mocker.patch("otaclient._otaproxy_ctx.otaproxy_control_thread")
        mocker.patch("otaclient.grpc.api_v2.main.grpc_server_process")
        mocker.patch("otaclient.ota_core.ota_core_process")

        # Mock os.execve to prevent process replacement
        mocker.patch(f"{MAIN_MODULE}.os.execve")

        # Mock _env.is_dynamic_client_preparing and running to control flow
        mocker.patch(
            "otaclient_common._env.is_dynamic_client_preparing", return_value=False
        )
        mocker.patch(
            "otaclient_common._env.is_dynamic_client_running", return_value=False
        )

        # Mock ClientUpdateControlFlags
        mock_client_update_flags = mocker.MagicMock()
        mock_client_update_flags.notify_data_ready_event.is_set.return_value = False
        mock_client_update_flags.request_shutdown_event.is_set.return_value = False
        mocker.patch(
            f"{MAIN_MODULE}.ClientUpdateControlFlags",
            return_value=mock_client_update_flags,
        )

        # Create mock for otaproxy thread
        mock_thread = mocker.MagicMock()
        mocker.patch(f"{MAIN_MODULE}.threading.Thread", return_value=mock_thread)

        # Mock multiprocessing context and processes
        mock_mp_ctx = mocker.MagicMock()
        mock_mp_ctx.Queue.side_effect = [MagicMock(), MagicMock()]
        mock_mp_ctx.Event.side_effect = [
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
        ]
        mock_mp_ctx.Process.side_effect = [
            self.mock_ota_core_p,
            self.mock_grpc_server_p,
        ]
        mocker.patch(f"{MAIN_MODULE}.mp.get_context", return_value=mock_mp_ctx)

        # Execute main() and catch the exception we use to break the loop
        try:
            main.main()
        except Exception:
            pass

        # Verify processes were started
        self.mock_ota_core_p.start.assert_called_once()
        self.mock_grpc_server_p.start.assert_called_once()

        # Verify thread was started if proxy_info.enable_local_ota_proxy is True
        # Note: In setup, we mocked proxy_info.enable_local_ota_proxy to False
        # so thread should not be started
        mock_thread.start.assert_not_called()

    @patch("otaclient._logging.configure_logging")
    def test_main_api_server_not_alive(
        self, mock_logging, mocker: pytest_mock.MockerFixture
    ):
        """Test main function when API server process dies."""
        # Mock the modules and functions imported in main()
        mocker.patch("otaclient._otaproxy_ctx.otaproxy_control_thread")
        mocker.patch("otaclient.grpc.api_v2.main.grpc_server_process")
        mocker.patch("otaclient.ota_core.ota_core_process")

        # Mock os.execve to prevent process replacement
        mocker.patch(f"{MAIN_MODULE}.os.execve")

        # Mock _env.is_dynamic_client_preparing and running to control flow
        mocker.patch(
            "otaclient_common._env.is_dynamic_client_preparing", return_value=False
        )
        mocker.patch(
            "otaclient_common._env.is_dynamic_client_running", return_value=False
        )

        # Mock ClientUpdateControlFlags
        mock_client_update_flags = mocker.MagicMock()
        mock_client_update_flags.notify_data_ready_event.is_set.return_value = False
        mock_client_update_flags.request_shutdown_event.is_set.return_value = False
        mocker.patch(
            f"{MAIN_MODULE}.ClientUpdateControlFlags",
            return_value=mock_client_update_flags,
        )

        # Mock multiprocessing context and processes
        mock_mp_ctx = mocker.MagicMock()
        mock_mp_ctx.Queue.side_effect = [MagicMock(), MagicMock()]
        mock_mp_ctx.Event.side_effect = [
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
        ]
        mock_mp_ctx.Process.side_effect = [
            self.mock_ota_core_p,
            self.mock_grpc_server_p,
        ]
        mocker.patch(f"{MAIN_MODULE}.mp.get_context", return_value=mock_mp_ctx)

        # Set up health check behavior
        # Skip time.sleep to make it fast
        mocker.patch(f"{MAIN_MODULE}.time.sleep")

        # Set is_alive for processes - grpc server is not alive
        self.mock_ota_core_p.is_alive.return_value = True
        self.mock_grpc_server_p.is_alive.return_value = False

        # Mock _on_shutdown to clean up after the test
        mock_on_shutdown = mocker.patch(f"{MAIN_MODULE}._on_shutdown")

        # Execute main()
        main.main()

        # Verify _on_shutdown was called because grpc server died
        mock_on_shutdown.assert_called_once()

    @pytest.mark.parametrize(
        "is_preparing, is_running",
        [
            (True, False),  # Only preparing
            (False, True),  # Only running
            (True, True),  # Both preparing and running (unexpected state)
        ],
    )
    @patch("otaclient._logging.configure_logging")
    def test_main_dynamic_client_flags(
        self, mock_logging, is_preparing, is_running, mocker: pytest_mock.MockerFixture
    ):
        """Test main function with different dynamic client flag combinations."""
        # Mock is_dynamic_client_preparing and is_dynamic_client_running
        mocker.patch(
            "otaclient_common._env.is_dynamic_client_preparing",
            return_value=is_preparing,
        )
        mocker.patch(
            "otaclient_common._env.is_dynamic_client_running", return_value=is_running
        )

        # Mock the OTAClientPackagePreparer to prevent actual mounting
        mock_prepareter = mocker.MagicMock()
        mocker.patch(
            "otaclient.client_package.OTAClientPackagePreparer",
            return_value=mock_prepareter,
        )

        # Mock os functions to prevent actual system changes
        mock_chroot = mocker.patch(f"{MAIN_MODULE}.os.chroot")
        mock_chdir = mocker.patch(f"{MAIN_MODULE}.os.chdir")

        # Mock execve to prevent actual process replacement
        mock_execve = mocker.patch(f"{MAIN_MODULE}.os.execve")
        mocker.patch(f"{MAIN_MODULE}.sys.exit")

        mock_environ = {"PATH": "/usr/bin"}
        if is_preparing:
            mock_environ[cfg.PREPARING_DOWNLOADED_DYNAMIC_OTA_CLIENT] = "yes"

        mocker.patch(f"{MAIN_MODULE}.os.environ.copy", return_value=mock_environ)
        # Mock _on_shutdown to prevent SystemExit
        mocker.patch(f"{MAIN_MODULE}._on_shutdown")
        # Patch check_other_otaclient at the module level where it's imported
        mock_check_other_otaclient = mocker.patch(
            "otaclient._utils.check_other_otaclient"
        )

        # Execute main()
        main.main()

        # Verify behavior based on flag values
        if is_preparing:
            # Check preparation path is taken when is_preparing=True
            mock_prepareter.mount_client_package.assert_called_once()
            mock_chroot.assert_called_once_with(cfg.DYNAMIC_CLIENT_MNT)
            mock_chdir.assert_called_once_with("/")
            mock_execve.assert_called_once()
            mock_check_other_otaclient.assert_not_called()
        elif is_running:
            # Check preparation path is not taken when is_preparing=False
            mock_prepareter.mount_client_package.assert_not_called()
            mock_chroot.assert_not_called()
            mock_chdir.assert_not_called()
            mock_execve.assert_not_called()
            mock_check_other_otaclient.assert_not_called()
