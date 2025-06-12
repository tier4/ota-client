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

import multiprocessing as mp
import threading
import time
from functools import partial
from pathlib import Path
from unittest.mock import patch

from ota_proxy import run_otaproxy
from otaclient._otaproxy_ctx import (
    otaproxy_control_thread,
    otaproxy_on_global_shutdown,
)
from otaclient._types import MultipleECUStatusFlags


def otaproxy_process(cache_dir: str):
    ota_cache_dir = Path(cache_dir)
    ota_cache_db = ota_cache_dir / "cache_db"

    run_otaproxy(
        host="127.0.0.1",
        port=8082,
        init_cache=True,
        cache_dir=str(ota_cache_dir),
        cache_db_f=str(ota_cache_db),
        upper_proxy="",
        enable_cache=True,
        enable_https=False,
    )


def test_subprocess_start_otaproxy(tmp_path: Path):
    # --- setup --- #
    (ota_cache_dir := tmp_path / "ota-cache").mkdir(exist_ok=True)
    ota_cache_db = ota_cache_dir / "cache_db"

    # --- execution --- #
    _spawn_ctx = mp.get_context("spawn")

    otaproxy_subprocess = _spawn_ctx.Process(
        target=otaproxy_process, args=(ota_cache_dir,)
    )
    otaproxy_subprocess.start()
    time.sleep(3)  # wait for subprocess to finish up initializing

    # --- assertion --- #
    try:
        assert otaproxy_subprocess.is_alive()
        assert ota_cache_db.is_file()
    finally:
        otaproxy_subprocess.terminate()
        otaproxy_subprocess.join()


def test_otaproxy_shutdown_race_condition():
    """Test that otaproxy doesn't start after global shutdown is called."""

    # Reset global state first
    import otaclient._otaproxy_ctx as ctx

    ctx._global_shutdown.clear()
    ctx._otaproxy_p = None

    # Setup multiprocessing context and flags
    mp_ctx = mp.get_context("spawn")
    ecu_status_flags = MultipleECUStatusFlags(
        any_child_ecu_in_update=mp_ctx.Event(),
        any_requires_network=mp_ctx.Event(),
        all_success=mp_ctx.Event(),
    )

    # Set flags to trigger otaproxy startup
    ecu_status_flags.any_requires_network.set()

    # Mock the Process creation at the right place
    with patch("multiprocessing.get_context") as mock_get_context, patch(
        "otaclient._otaproxy_ctx.otaproxy_process"
    ) as mock_process, patch("otaclient._otaproxy_ctx.OTAPROXY_CHECK_INTERVAL", 0.1):

        # Setup mock multiprocessing context
        mock_mp_ctx = mock_get_context.return_value
        mock_process_instance = mock_mp_ctx.Process.return_value
        mock_process_instance.is_alive.return_value = True

        # Start control thread
        control_thread = threading.Thread(
            target=partial(otaproxy_control_thread, ecu_status_flags), daemon=True
        )
        control_thread.start()

        # Wait for startup attempts
        time.sleep(1)  # Wait longer than OTAPROXY_CHECK_INTERVAL

        # Verify process creation was attempted
        assert (
            mock_mp_ctx.Process.call_count >= 1
        ), f"Expected process to be created at least once, got {mock_mp_ctx.Process.call_count}"

        # Reset mocks for the shutdown test
        mock_mp_ctx.Process.reset_mock()
        mock_process.reset_mock()

        # Call global shutdown
        otaproxy_on_global_shutdown(blocking=True)

        # Wait to ensure shutdown signal is processed
        time.sleep(1)

        # Clear and set network flag again to trigger potential startup
        ecu_status_flags.any_requires_network.clear()
        time.sleep(0.1)
        ecu_status_flags.any_requires_network.set()

        # Wait more than OTAPROXY_CHECK_INTERVAL to allow potential startup
        time.sleep(1)

        # Assert that no new otaproxy process was started after shutdown
        mock_mp_ctx.Process.assert_not_called()

        # Clean up
        control_thread.join(timeout=1)


def test_otaproxy_immediate_shutdown_prevents_startup():
    """Test that calling shutdown immediately prevents any otaproxy startup."""

    # Reset global state first
    import otaclient._otaproxy_ctx as ctx

    ctx._global_shutdown.clear()
    ctx._otaproxy_p = None

    mp_ctx = mp.get_context("spawn")
    ecu_status_flags = MultipleECUStatusFlags(
        any_child_ecu_in_update=mp_ctx.Event(),
        any_requires_network=mp_ctx.Event(),
        all_success=mp_ctx.Event(),
    )

    with patch("multiprocessing.get_context") as mock_get_context, patch(
        "otaclient._otaproxy_ctx.otaproxy_process"
    ), patch("otaclient._otaproxy_ctx.OTAPROXY_CHECK_INTERVAL", 1):

        # Setup mock multiprocessing context
        mock_mp_ctx = mock_get_context.return_value
        mock_process_instance = mock_mp_ctx.Process.return_value
        mock_process_instance.is_alive.return_value = False

        # Call shutdown BEFORE starting control thread
        otaproxy_on_global_shutdown(blocking=True)

        # Set flags that would trigger startup
        ecu_status_flags.any_requires_network.set()

        # Start control thread
        control_thread = threading.Thread(
            target=partial(otaproxy_control_thread, ecu_status_flags), daemon=True
        )
        control_thread.start()

        # Wait more than OTAPROXY_CHECK_INTERVAL
        time.sleep(1)

        # Assert that otaproxy was never started
        mock_mp_ctx.Process.assert_not_called()

        # Clean up
        control_thread.join(timeout=1)


def test_otaproxy_control_thread_exits_on_shutdown():
    """Test that control thread exits properly when shutdown is called."""

    # Reset global state first
    import otaclient._otaproxy_ctx as ctx

    ctx._global_shutdown.clear()
    ctx._otaproxy_p = None

    mp_ctx = mp.get_context("spawn")
    ecu_status_flags = MultipleECUStatusFlags(
        any_child_ecu_in_update=mp_ctx.Event(),
        any_requires_network=mp_ctx.Event(),
        all_success=mp_ctx.Event(),
    )

    with patch("otaclient._otaproxy_ctx.otaproxy_process"):
        # Start control thread
        control_thread = threading.Thread(
            target=partial(otaproxy_control_thread, ecu_status_flags), daemon=True
        )
        control_thread.start()

        # Verify thread is running
        time.sleep(1)
        assert control_thread.is_alive()

        # Call shutdown
        otaproxy_on_global_shutdown(blocking=True)

        # Wait for thread to exit
        control_thread.join(timeout=5)

        # Verify thread has exited
        assert not control_thread.is_alive()
