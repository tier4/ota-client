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
        mocker.patch("otaclient._otaproxy_ctx.otaproxy_control_thread")
        mocker.patch("otaclient.grpc.api_v2.main.grpc_server_process")
        mocker.patch("otaclient.ota_core.ota_core_process")
        mocker.patch("otaclient._stop_monitor.stop_request_thread")

        mocker.patch(f"{MAIN_MODULE}.os.execve")

        mocker.patch(
            "otaclient_common._env.is_dynamic_client_preparing", return_value=False
        )
        mocker.patch(
            "otaclient_common._env.is_dynamic_client_running", return_value=False
        )

        mock_client_update_flags = mocker.MagicMock()
        mock_client_update_flags.notify_data_ready_event.is_set.return_value = False
        mock_client_update_flags.request_shutdown_event.is_set.return_value = False
        mocker.patch(
            f"{MAIN_MODULE}.ClientUpdateControlFlags",
            return_value=mock_client_update_flags,
        )

        mock_stop_thread = mocker.MagicMock()
        mock_stop_thread.is_alive.side_effect = [True, False]
        mock_thread = mocker.MagicMock()
        mocker.patch(
            f"{MAIN_MODULE}.threading.Thread",
            side_effect=[mock_thread, mock_stop_thread],
        )

        mock_mp_ctx = mocker.MagicMock()
        mock_mp_ctx.Queue.side_effect = [MagicMock(), MagicMock(), MagicMock()]
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

        mock_on_shutdown = mocker.patch(f"{MAIN_MODULE}._on_shutdown")

        main.main()

        self.mock_ota_core_p.start.assert_called_once()
        self.mock_grpc_server_p.start.assert_called_once()
        mock_stop_thread.start.assert_called_once()
        mock_on_shutdown.assert_called_once_with(sys_exit=True)

    @patch("otaclient._logging.configure_logging")
    def test_main_api_server_not_alive(
        self, mock_logging, mocker: pytest_mock.MockerFixture
    ):
        """Test main function when API server process dies."""
        mocker.patch("otaclient._otaproxy_ctx.otaproxy_control_thread")
        mocker.patch("otaclient.grpc.api_v2.main.grpc_server_process")
        mocker.patch("otaclient.ota_core.ota_core_process")
        mocker.patch("otaclient._stop_monitor.stop_request_thread")

        mocker.patch(f"{MAIN_MODULE}.os.execve")

        mocker.patch(
            "otaclient_common._env.is_dynamic_client_preparing", return_value=False
        )
        mocker.patch(
            "otaclient_common._env.is_dynamic_client_running", return_value=False
        )

        mock_client_update_flags = mocker.MagicMock()
        mock_client_update_flags.notify_data_ready_event.is_set.return_value = False
        mock_client_update_flags.request_shutdown_event.is_set.return_value = False
        mocker.patch(
            f"{MAIN_MODULE}.ClientUpdateControlFlags",
            return_value=mock_client_update_flags,
        )

        mock_stop_thread = mocker.MagicMock()
        mock_stop_thread.is_alive.return_value = True
        mock_thread = mocker.MagicMock()
        mocker.patch(
            f"{MAIN_MODULE}.threading.Thread",
            side_effect=[mock_thread, mock_stop_thread],
        )

        mock_mp_ctx = mocker.MagicMock()
        mock_mp_ctx.Queue.side_effect = [MagicMock(), MagicMock(), MagicMock()]
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

        mocker.patch(f"{MAIN_MODULE}.time.sleep")

        self.mock_ota_core_p.is_alive.return_value = True
        self.mock_grpc_server_p.is_alive.return_value = False

        mock_on_shutdown = mocker.patch(f"{MAIN_MODULE}._on_shutdown")

        main.main()

        mock_on_shutdown.assert_called_once()

    def test_shutdown_function(self, mocker: pytest_mock.MockerFixture):
        """Test the public shutdown function."""
        mock_on_shutdown = mocker.patch(f"{MAIN_MODULE}._on_shutdown")

        main.shutdown(sys_exit=False)
        mock_on_shutdown.assert_called_once_with(sys_exit=False)

        mock_on_shutdown.reset_mock()

        main.shutdown(sys_exit=True)
        mock_on_shutdown.assert_called_once_with(sys_exit=True)

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

    @patch("otaclient._logging.configure_logging")
    def test_main_stop_request_thread_functionality(
        self, mock_logging, mocker: pytest_mock.MockerFixture
    ):
        """Test main function with stop request thread integration."""
        # Mock the modules and functions imported in main()
        mocker.patch("otaclient._otaproxy_ctx.otaproxy_control_thread")
        mocker.patch("otaclient.grpc.api_v2.main.grpc_server_process")
        mocker.patch("otaclient.ota_core.ota_core_process")
        mocker.patch("otaclient._stop_monitor.stop_request_thread")

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

        # Create mock for stop request thread
        mock_stop_thread = mocker.MagicMock()
        mock_stop_thread.is_alive.return_value = (
            False  # Thread finished (stop request received)
        )
        mocker.patch(f"{MAIN_MODULE}.threading.Thread", return_value=mock_stop_thread)

        # Mock multiprocessing context and processes
        mock_mp_ctx = mocker.MagicMock()
        mock_mp_ctx.Queue.side_effect = [MagicMock(), MagicMock(), MagicMock()]
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

        # Mock _on_shutdown to capture the shutdown call
        mock_on_shutdown = mocker.patch(f"{MAIN_MODULE}._on_shutdown")

        # Execute main()
        main.main()

        # Verify stop request thread was started
        mock_stop_thread.start.assert_called_once()
        mock_stop_thread.join.assert_called_once_with(main.HEALTH_CHECK_INTERVAL)

        # Verify _on_shutdown was called because stop thread finished
        mock_on_shutdown.assert_called_once_with(sys_exit=True)

    @patch("otaclient._logging.configure_logging")
    def test_main_stop_request_thread_still_alive_health_checks_run(
        self, mock_logging, mocker: pytest_mock.MockerFixture
    ):
        """Test main function when stop request thread is still alive and health checks run."""
        # Mock the modules and functions imported in main()
        mocker.patch("otaclient._otaproxy_ctx.otaproxy_control_thread")
        mocker.patch("otaclient.grpc.api_v2.main.grpc_server_process")
        mocker.patch("otaclient.ota_core.ota_core_process")
        mocker.patch("otaclient._stop_monitor.stop_request_thread")

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

        # Create mock for stop request thread - alive initially, then dies on second check
        mock_stop_thread = mocker.MagicMock()
        mock_stop_thread.is_alive.side_effect = [
            True,
            False,
        ]  # Alive first time, dead second time
        mocker.patch(f"{MAIN_MODULE}.threading.Thread", return_value=mock_stop_thread)

        # Mock multiprocessing context and processes
        mock_mp_ctx = mocker.MagicMock()
        mock_mp_ctx.Queue.side_effect = [MagicMock(), MagicMock(), MagicMock()]
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

        # Mock _on_shutdown to capture the shutdown call
        mock_on_shutdown = mocker.patch(f"{MAIN_MODULE}._on_shutdown")

        # Execute main()
        main.main()

        # Verify stop request thread was checked twice
        assert mock_stop_thread.join.call_count == 2
        assert mock_stop_thread.is_alive.call_count == 2

        # Verify health checks ran (is_alive called on processes)
        self.mock_ota_core_p.is_alive.assert_called()
        self.mock_grpc_server_p.is_alive.assert_called()

        # Verify _on_shutdown was called when stop thread died
        mock_on_shutdown.assert_called_once_with(sys_exit=True)

    @patch("otaclient._logging.configure_logging")
    def test_main_ota_core_process_not_alive_with_stop_thread_running(
        self, mock_logging, mocker: pytest_mock.MockerFixture
    ):
        """Test main function when ota core process dies while stop thread is running."""
        # Mock the modules and functions imported in main()
        mocker.patch("otaclient._otaproxy_ctx.otaproxy_control_thread")
        mocker.patch("otaclient.grpc.api_v2.main.grpc_server_process")
        mocker.patch("otaclient.ota_core.ota_core_process")
        mocker.patch("otaclient._stop_monitor.stop_request_thread")

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

        # Create mock for stop request thread - always alive
        mock_stop_thread = mocker.MagicMock()
        mock_stop_thread.is_alive.return_value = True
        mocker.patch(f"{MAIN_MODULE}.threading.Thread", return_value=mock_stop_thread)

        # Mock multiprocessing context and processes
        mock_mp_ctx = mocker.MagicMock()
        mock_mp_ctx.Queue.side_effect = [MagicMock(), MagicMock(), MagicMock()]
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

        # Set up health check behavior - ota core process is dead
        self.mock_ota_core_p.is_alive.return_value = False
        self.mock_grpc_server_p.is_alive.return_value = True

        # Mock time.sleep to make test faster
        mocker.patch(f"{MAIN_MODULE}.time.sleep")

        # Mock _on_shutdown to capture the shutdown call
        mock_on_shutdown = mocker.patch(f"{MAIN_MODULE}._on_shutdown")

        # Execute main()
        main.main()

        # Verify stop thread join was called
        mock_stop_thread.join.assert_called_with(main.HEALTH_CHECK_INTERVAL)

        # Verify ota core process health was checked
        self.mock_ota_core_p.is_alive.assert_called()

        # Verify _on_shutdown was called because ota core died
        mock_on_shutdown.assert_called_once_with(sys_exit=True)

    @patch("otaclient._logging.configure_logging")
    def test_main_notify_data_ready_event_with_stop_thread_running(
        self, mock_logging, mocker: pytest_mock.MockerFixture
    ):
        """Test main function when notify_data_ready_event is set while stop thread is running."""
        # Mock the modules and functions imported in main()
        mocker.patch("otaclient._otaproxy_ctx.otaproxy_control_thread")
        mocker.patch("otaclient._otaproxy_ctx.otaproxy_on_global_shutdown")
        mocker.patch("otaclient.grpc.api_v2.main.grpc_server_process")
        mocker.patch("otaclient.ota_core.ota_core_process")
        mocker.patch("otaclient._stop_monitor.stop_request_thread")

        # Mock os.execve to prevent process replacement
        mock_execve = mocker.patch(f"{MAIN_MODULE}.os.execve")

        # Mock _env.is_dynamic_client_preparing and running to control flow
        mocker.patch(
            "otaclient_common._env.is_dynamic_client_preparing", return_value=False
        )
        mocker.patch(
            "otaclient_common._env.is_dynamic_client_running", return_value=False
        )

        # Mock ClientUpdateControlFlags with notify_data_ready_event set
        mock_client_update_flags = mocker.MagicMock()
        mock_client_update_flags.notify_data_ready_event.is_set.return_value = True
        mock_client_update_flags.request_shutdown_event.is_set.return_value = False
        mocker.patch(
            f"{MAIN_MODULE}.ClientUpdateControlFlags",
            return_value=mock_client_update_flags,
        )

        # Create mock for stop request thread - always alive
        mock_stop_thread = mocker.MagicMock()
        mock_stop_thread.is_alive.return_value = True
        mocker.patch(f"{MAIN_MODULE}.threading.Thread", return_value=mock_stop_thread)

        # Mock multiprocessing context and processes
        mock_mp_ctx = mocker.MagicMock()
        mock_mp_ctx.Queue.side_effect = [MagicMock(), MagicMock(), MagicMock()]
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

        # Mock _on_shutdown
        mock_on_shutdown = mocker.patch(f"{MAIN_MODULE}._on_shutdown")

        # Mock environment copy
        mock_environ = {"PATH": "/usr/bin"}
        mocker.patch(f"{MAIN_MODULE}.os.environ.copy", return_value=mock_environ)

        # Execute main()
        main.main()

        # Verify stop thread was started and checked
        mock_stop_thread.start.assert_called_once()
        mock_stop_thread.join.assert_called_with(main.HEALTH_CHECK_INTERVAL)

        # Verify _on_shutdown was called to clean up resources
        mock_on_shutdown.assert_called_once_with(sys_exit=False)

        # Verify execve was called for dynamic client preparation
        mock_execve.assert_called_once()
        preparing_env = mock_execve.call_args[1]["env"]
        assert cfg.PREPARING_DOWNLOADED_DYNAMIC_OTA_CLIENT in preparing_env

    @patch("otaclient._logging.configure_logging")
    def test_main_request_shutdown_event_with_stop_thread_running(
        self, mock_logging, mocker: pytest_mock.MockerFixture
    ):
        """Test main function when request_shutdown_event is set while stop thread is running."""
        # Mock the modules and functions imported in main()
        mocker.patch("otaclient._otaproxy_ctx.otaproxy_control_thread")
        mocker.patch("otaclient.grpc.api_v2.main.grpc_server_process")
        mocker.patch("otaclient.ota_core.ota_core_process")
        mocker.patch("otaclient._stop_monitor.stop_request_thread")

        # Mock os.execve to prevent process replacement
        mocker.patch(f"{MAIN_MODULE}.os.execve")

        # Mock _env.is_dynamic_client_preparing and running to control flow
        mocker.patch(
            "otaclient_common._env.is_dynamic_client_preparing", return_value=False
        )
        mocker.patch(
            "otaclient_common._env.is_dynamic_client_running", return_value=False
        )

        # Mock ClientUpdateControlFlags with request_shutdown_event set
        mock_client_update_flags = mocker.MagicMock()
        mock_client_update_flags.notify_data_ready_event.is_set.return_value = False
        mock_client_update_flags.request_shutdown_event.is_set.return_value = True
        mocker.patch(
            f"{MAIN_MODULE}.ClientUpdateControlFlags",
            return_value=mock_client_update_flags,
        )

        # Create mock for stop request thread - always alive
        mock_stop_thread = mocker.MagicMock()
        mock_stop_thread.is_alive.return_value = True
        mocker.patch(f"{MAIN_MODULE}.threading.Thread", return_value=mock_stop_thread)

        # Mock multiprocessing context and processes
        mock_mp_ctx = mocker.MagicMock()
        mock_mp_ctx.Queue.side_effect = [MagicMock(), MagicMock(), MagicMock()]
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

        # Mock _on_shutdown to capture the shutdown call
        mock_on_shutdown = mocker.patch(f"{MAIN_MODULE}._on_shutdown")

        # Execute main()
        main.main()

        # Verify stop thread was started and checked
        mock_stop_thread.start.assert_called_once()
        mock_stop_thread.join.assert_called_with(main.HEALTH_CHECK_INTERVAL)

        # Verify _on_shutdown was called because shutdown was requested
        mock_on_shutdown.assert_called_once_with(sys_exit=True)
