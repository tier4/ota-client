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
"""Top-level test helpers shared across unit / integration / e2e tiers.

Currently provides:
    - ``ota_status_collector``: class-scoped ``OTAClientStatusCollector``
      thread + in-process report queue, used by ``_status_monitor`` and
      OTAClient IPC tests.
    - ``launch_http_server_subprocess``: context manager that spawns
      ``_utils/_http_server.py`` as a child process serving a given
      directory and yields the base URL. Use this whenever a test needs
      a real HTTP boundary between the system-under-test and a fixture
      tree.

Standalone helper scripts (anything invoked as a subprocess rather than
imported) live under ``test/_utils/``.
"""

from __future__ import annotations

import logging
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from queue import Queue
from typing import Any, Generator, Iterator

import pytest
import pytest_mock

from otaclient._status_monitor import (
    TERMINATE_SENTINEL,
    OTAClientStatusCollector,
    StatusReport,
)

logger = logging.getLogger(__name__)

MAX_TRACEBACK_SIZE = 2048

_HTTP_SERVER_SCRIPT = Path(__file__).parent / "_utils" / "_http_server.py"
_READY_PREFIX = "READY:"


@pytest.fixture(scope="class")
def ota_status_collector(
    class_mocker: pytest_mock.MockerFixture,
) -> Generator[tuple[OTAClientStatusCollector, Queue[StatusReport]], Any, None]:
    """Class-scoped status collector with an in-process report queue.

    Used by `_status_monitor` and OTAClient IPC tests that need to push
    status reports through a real `OTAClientStatusCollector` thread.
    """
    _shm_mock = class_mocker.MagicMock()

    _report_queue: Queue[StatusReport] = Queue()
    _status_collector = OTAClientStatusCollector(
        msg_queue=_report_queue,
        shm_status=_shm_mock,
        max_traceback_size=MAX_TRACEBACK_SIZE,
    )
    _collector_thread = _status_collector.start()

    try:
        yield _status_collector, _report_queue
    finally:
        _report_queue.put_nowait(TERMINATE_SENTINEL)
        _collector_thread.join()


def _wait_for_http_ready(proc: subprocess.Popen, timeout: float) -> None:
    """Block until the spawned server prints its READY line."""
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"HTTP server exited prematurely with code {proc.returncode}"
                )
            time.sleep(0.1)
            continue
        if line.startswith(_READY_PREFIX):
            return
    raise TimeoutError("Timed out waiting for HTTP server READY signal")


@contextmanager
def launch_http_server_subprocess(
    host: str,
    port: int,
    directory: str | Path,
    *,
    ready_timeout: float = 30.0,
    shutdown_timeout: float = 10.0,
) -> Iterator[str]:
    """Spawn ``_http_server.py`` as a subprocess and yield its base URL.

    Args:
        host: Address to bind. Use ``"127.0.0.1"`` for loopback-only.
        port: TCP port to listen on.
        directory: Filesystem root to serve.
        ready_timeout: Max seconds to wait for the READY signal.
        shutdown_timeout: Max seconds to wait for graceful SIGINT shutdown
            before falling back to SIGKILL.

    Yields:
        Base URL of the server (e.g. ``"http://127.0.0.1:8080"``).
    """
    proc = subprocess.Popen(
        [
            sys.executable,
            str(_HTTP_SERVER_SCRIPT),
            "--host",
            host,
            "--port",
            str(port),
            "--directory",
            str(directory),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    base_url = f"http://{host}:{port}"
    try:
        _wait_for_http_ready(proc, ready_timeout)
        logger.info(f"HTTP server started at {base_url} (directory={directory})")
        yield base_url
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=shutdown_timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        logger.info(f"HTTP server stopped (directory={directory})")
