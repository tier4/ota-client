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
from pathlib import Path

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
        mock_proxy_info = mocker.MagicMock()
        mock_proxy_info.enable_local_ota_proxy = False
        mock_proxy_info.local_ota_proxy = {}
        mocker.patch("otaclient.configs.cfg.proxy_info", mock_proxy_info)

        # Mock common functions to prevent side effects
        mocker.patch(f"{MAIN_MODULE}.secrets.token_bytes", return_value=b"mock_key")
        mocker.patch(f"{MAIN_MODULE}.time.sleep")
        mocker.patch(f"{MAIN_MODULE}.atexit.register")
        mocker.patch(f"{MAIN_MODULE}.signal.signal")

        mocker.patch("otaclient._utils.check_other_otaclient")

        self.mock_ota_core_p = mocker.MagicMock()
        self.mock_grpc_server_p = mocker.MagicMock()

        self.mock_ota_core_p.is_alive.return_value = True
        self.mock_grpc_server_p.is_alive.return_value = True

    def test_on_shutdown(self, mocker: pytest_mock.MockerFixture):
        """Test the _on_shutdown function."""
        sys_exit_mock = mocker.patch(f"{MAIN_MODULE}.sys.exit")

        _shutdown_lock_mock = mocker.MagicMock()
        _shutdown_lock_mock.acquire.return_value = True
        mocker.patch(f"{MAIN_MODULE}._global_shutdown_lock", _shutdown_lock_mock)

        main._ota_core_p = self.mock_ota_core_p
        main._grpc_server_p = self.mock_grpc_server_p
        main._shm = self.mock_shm

        getpgid_mock = mocker.patch(f"{MAIN_MODULE}.os.getpgid", return_value=4321)

        # Call with sys_exit=False
        main._on_shutdown(sys_exit=False)

        self.mock_ota_core_p.terminate.assert_called_once()
        self.mock_ota_core_p.join.assert_called_once()
        self.mock_grpc_server_p.terminate.assert_called_once()
        self.mock_grpc_server_p.join.assert_called_once()

        self.mock_shm.close.assert_called_once()
        self.mock_shm.unlink.assert_called_once()

        sys_exit_mock.assert_not_called()

        # Reset for second call
        self.mock_ota_core_p.reset_mock()
        self.mock_grpc_server_p.reset_mock()
        self.mock_shm.reset_mock()
        getpgid_mock.reset_mock()

        main._ota_core_p = self.mock_ota_core_p
        main._grpc_server_p = self.mock_grpc_server_p
        main._shm = self.mock_shm

        # Call with sys_exit=True
        main._on_shutdown(sys_exit=True)

        self.mock_ota_core_p.terminate.assert_called_once()
        self.mock_ota_core_p.join.assert_called_once()
        self.mock_grpc_server_p.terminate.assert_called_once()
        self.mock_grpc_server_p.join.assert_called_once()

        self.mock_shm.close.assert_called_once()
        self.mock_shm.unlink.assert_called_once()

        sys_exit_mock.assert_called_once_with(1)

    def test_signal_handler(self, mocker: pytest_mock.MockerFixture):
        """Test the _signal_handler function."""
        on_shutdown_mock = mocker.patch(f"{MAIN_MODULE}._on_shutdown")

        main._signal_handler(signal.SIGTERM, None)

        on_shutdown_mock.assert_called_once_with()

    def test_main_process_setup_and_health_check(
        self, mocker: pytest_mock.MockerFixture
    ):
        """Test the main function process setup and health check loop."""
        mocker.patch("otaclient._logging.configure_logging")

        # Skip the full execution of main() by making time.sleep raise after one call
        mocker.patch(
            f"{MAIN_MODULE}.time.sleep", side_effect=[None, Exception("Break loop")]
        )

        mocker.patch("otaclient._otaproxy_ctx.otaproxy_control_thread")
        mocker.patch("otaclient.grpc.api_v2.main.grpc_server_process")
        mocker.patch("otaclient.ota_core.ota_core_process")
        mocker.patch(f"{MAIN_MODULE}.os.execve")
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

        mock_thread = mocker.MagicMock()
        mocker.patch(f"{MAIN_MODULE}.threading.Thread", return_value=mock_thread)

        mock_mp_ctx = mocker.MagicMock()
        mock_mp_ctx.Queue.side_effect = [
            mocker.MagicMock(),  # local_otaclient_op_queue
            mocker.MagicMock(),  # local_otaclient_resp_queue
        ]
        mock_mp_ctx.Event.side_effect = [
            mocker.MagicMock(),  # any_child_ecu_in_update
            mocker.MagicMock(),  # any_requires_network
            mocker.MagicMock(),  # all_success
            mocker.MagicMock(),  # notify_data_ready_event
            mocker.MagicMock(),  # request_shutdown_event
        ]
        mock_mp_ctx.Process.side_effect = [
            self.mock_ota_core_p,
            self.mock_grpc_server_p,
        ]
        mocker.patch(f"{MAIN_MODULE}.mp.get_context", return_value=mock_mp_ctx)

        try:
            main.main()
        except Exception:
            pass

        self.mock_ota_core_p.start.assert_called_once()
        self.mock_grpc_server_p.start.assert_called_once()

        # proxy_info.enable_local_ota_proxy is False so thread shouldn't start
        mock_thread.start.assert_not_called()

    def test_main_api_server_not_alive(self, mocker: pytest_mock.MockerFixture):
        """Test main function when API server process dies."""
        mocker.patch("otaclient._logging.configure_logging")
        mocker.patch("otaclient._otaproxy_ctx.otaproxy_control_thread")
        mocker.patch("otaclient.grpc.api_v2.main.grpc_server_process")
        mocker.patch("otaclient.ota_core.ota_core_process")
        mocker.patch(f"{MAIN_MODULE}.os.execve")
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

        mock_mp_ctx = mocker.MagicMock()
        mock_mp_ctx.Queue.side_effect = [
            mocker.MagicMock(),
            mocker.MagicMock(),
        ]
        mock_mp_ctx.Event.side_effect = [
            mocker.MagicMock(),
            mocker.MagicMock(),
            mocker.MagicMock(),
            mocker.MagicMock(),
            mocker.MagicMock(),
        ]
        mock_mp_ctx.Process.side_effect = [
            self.mock_ota_core_p,
            self.mock_grpc_server_p,
        ]
        mocker.patch(f"{MAIN_MODULE}.mp.get_context", return_value=mock_mp_ctx)

        mocker.patch(f"{MAIN_MODULE}.time.sleep")

        # grpc server is not alive
        self.mock_ota_core_p.is_alive.return_value = True
        self.mock_grpc_server_p.is_alive.return_value = False

        mock_on_shutdown = mocker.patch(f"{MAIN_MODULE}._on_shutdown")

        main.main()

        mock_on_shutdown.assert_called_once()

    def test_main_abort_shutdown(self, mocker: pytest_mock.MockerFixture):
        """Test main function shutdown when ota_core exits with EXIT_CODE_OTA_ABORTED."""
        mocker.patch("otaclient._logging.configure_logging")
        mocker.patch("otaclient._otaproxy_ctx.otaproxy_control_thread")
        mocker.patch("otaclient.grpc.api_v2.main.grpc_server_process")
        mocker.patch("otaclient.ota_core.ota_core_process")
        mocker.patch(f"{MAIN_MODULE}.os.execve")
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

        mock_mp_ctx = mocker.MagicMock()
        mock_mp_ctx.Queue.side_effect = [
            mocker.MagicMock(),
            mocker.MagicMock(),
        ]
        mock_mp_ctx.Event.side_effect = [
            mocker.MagicMock(),
            mocker.MagicMock(),
            mocker.MagicMock(),
            mocker.MagicMock(),
            mocker.MagicMock(),
        ]
        mock_mp_ctx.Process.side_effect = [
            self.mock_ota_core_p,
            self.mock_grpc_server_p,
        ]
        mocker.patch(f"{MAIN_MODULE}.mp.get_context", return_value=mock_mp_ctx)

        mock_sleep = mocker.patch(f"{MAIN_MODULE}.time.sleep")
        mock_on_shutdown = mocker.patch(f"{MAIN_MODULE}._on_shutdown")

        # Simulate ota_core exiting with EXIT_CODE_OTA_ABORTED (79)
        self.mock_ota_core_p.is_alive.return_value = False
        self.mock_ota_core_p.exitcode = 79  # EXIT_CODE_OTA_ABORTED

        main.main()

        mock_sleep.assert_any_call(main.SHUTDOWN_AFTER_ABORT_REQUEST_RECEIVED)
        mock_on_shutdown.assert_called_once()

    def test_main_dynamic_client_flags_otaclientupdate_app(
        self, mocker: pytest_mock.MockerFixture, monkeypatch: pytest.MonkeyPatch
    ):
        """Dynamic client running as otaclientupdate app skips both init and check."""
        mocker.patch("otaclient._logging.configure_logging")
        monkeypatch.setenv(cfg.RUNNING_DOWNLOADED_DYNAMIC_OTA_CLIENT, "yes")

        mocker.patch(
            "otaclient_common._env.is_running_as_downloaded_dynamic_app",
            return_value=True,
        )
        mocker.patch(f"{MAIN_MODULE}._on_shutdown")
        mock_check_other_otaclient = mocker.patch(
            "otaclient._utils.check_other_otaclient"
        )
        mock_dynamic_otaclient_init = mocker.patch(
            "otaclient.main._dynamic_otaclient_init"
        )

        main.main()

        mock_dynamic_otaclient_init.assert_not_called()
        mock_check_other_otaclient.assert_not_called()

    def test_main_dynamic_client_flags_running_as_app_image(
        self, mocker: pytest_mock.MockerFixture, monkeypatch: pytest.MonkeyPatch
    ):
        """Dynamic client running as app image triggers both init and check."""
        mocker.patch("otaclient._logging.configure_logging")
        monkeypatch.setenv(cfg.RUNNING_DOWNLOADED_DYNAMIC_OTA_CLIENT, "yes")

        mocker.patch(
            "otaclient_common._env.is_running_as_app_image",
            return_value=True,
        )
        mocker.patch(f"{MAIN_MODULE}._on_shutdown")
        mock_check_other_otaclient = mocker.patch(
            "otaclient._utils.check_other_otaclient"
        )
        mock_dynamic_otaclient_init = mocker.patch(
            "otaclient.main._dynamic_otaclient_init"
        )

        main.main()

        mock_dynamic_otaclient_init.assert_called_once()
        mock_check_other_otaclient.assert_called_once()


class TestBindExternalNFSCache:
    """Tests for the _bind_external_nfs_cache function."""

    def test_bind_external_nfs_cache_success(
        self, mocker: pytest_mock.MockerFixture, tmp_path: Path
    ):
        """Test successful binding of external NFS cache."""
        nfs_cache_mount_point = "/mnt/nfs_cache"
        host_root = Path(cfg.DYNAMIC_CLIENT_MNT_HOST_ROOT)
        host_root_nfs_cache = tmp_path / "host_nfs_cache"
        host_root_nfs_cache.mkdir(parents=True)

        mock_replace_root = mocker.patch(
            "otaclient.main.replace_root",
            return_value=str(host_root_nfs_cache),
        )
        mocker.patch("pathlib.Path.is_dir", return_value=True)
        mock_ensure_mount = mocker.patch("otaclient.main.ensure_mount")

        main._bind_external_nfs_cache(nfs_cache_mount_point)

        mock_replace_root.assert_called_once_with(
            nfs_cache_mount_point,
            cfg.CANONICAL_ROOT,
            host_root,
        )
        mock_ensure_mount.assert_called_once()

        call_args = mock_ensure_mount.call_args
        assert call_args.kwargs["target"] == host_root_nfs_cache
        assert call_args.kwargs["mnt_point"] == nfs_cache_mount_point
        assert call_args.kwargs["raise_exception"] is False

    def test_bind_external_nfs_cache_none(self, mocker: pytest_mock.MockerFixture):
        """Test when external_nfs_cache_mnt_point is None."""
        mock_ensure_mount = mocker.patch("otaclient.main.ensure_mount")

        main._bind_external_nfs_cache(None)

        mock_ensure_mount.assert_not_called()
