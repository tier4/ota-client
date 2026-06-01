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

import logging
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from functools import partial
from multiprocessing import Process
from pathlib import Path
from queue import Queue
from typing import TYPE_CHECKING, Any, Generator, Iterator, NamedTuple

import pytest
import pytest_mock

from otaclient import ota_core
from otaclient._status_monitor import (
    TERMINATE_SENTINEL,
    OTAClientStatusCollector,
    StatusReport,
)

if TYPE_CHECKING:
    from ota_image_libs.v1.file_table.db import FileTableDBHelper

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


# --------------------------------------------------------------------------- #
#   Shared OTA-image fixtures (container-baked /ota-image* and /certs*).
#
#   The /ota-image, /ota-image_v1, /certs and /certs_ota-image_v1 trees are
#   baked into the test container by docker/test_base/Dockerfile (copied from
#   the upstream ota_img_for_test images). Everything below is consumed only by
#   tests running inside that container. The fixtures are non-autouse, so they
#   never fire for tests that don't request them.
# --------------------------------------------------------------------------- #

# Baked into the test container image; see docker/test_base/Dockerfile.
OTA_IMAGE_DIR = Path("/ota-image")
OTA_IMAGE_DATA_DIR = OTA_IMAGE_DIR / "data"
CERTS_DIR = Path("/certs")
OTA_IMAGE_V1_DIR = Path("/ota-image_v1")
CERTS_OTA_IMAGE_V1_DIR = Path("/certs_ota-image_v1")

# Legacy OTA image CSV metadata fixtures (used to build file_table dbs).
REGULARS_TXT = OTA_IMAGE_DIR / "regulars.txt"
DIRS_TXT = OTA_IMAGE_DIR / "dirs.txt"
SYMLINKS_TXT = OTA_IMAGE_DIR / "symlinks.txt"

KERNEL_PREFIX = "vmlinuz"
INITRD_PREFIX = "initrd.img"

# Local OTA image HTTP servers, fronting the container-baked image dirs.
OTA_IMAGE_SERVER_ADDR = "127.0.0.1"
OTA_IMAGE_SERVER_PORT = 8080
OTA_IMAGE_URL = f"http://{OTA_IMAGE_SERVER_ADDR}:{OTA_IMAGE_SERVER_PORT}"
OTA_IMAGE_V1_SERVER_ADDR = "127.0.0.1"
OTA_IMAGE_V1_SERVER_PORT = 8081
OTA_IMAGE_V1_URL = f"http://{OTA_IMAGE_V1_SERVER_ADDR}:{OTA_IMAGE_V1_SERVER_PORT}"

CURRENT_VERSION = "123.x"
UPDATE_VERSION = "789.x"

OTA_UPDATER_MODULE = ota_core._updater.__name__


def _get_kernel_version() -> str:
    boot_dir = OTA_IMAGE_DIR / "data/boot"
    _kernel = next(iter(boot_dir.glob(f"{KERNEL_PREFIX}-*")))
    return _kernel.name.split("-", maxsplit=1)[1]


KERNEL_VERSION = _get_kernel_version()


class SlotMeta(NamedTuple):
    """A/B-slot layout for boot-controller-style updaters.

    Even for the grub controller (which doesn't use a separate boot dev
    in production), the test scaffolding always exposes a separate boot
    dev so the same fixture works for both grub and cboot schemes.
    """

    slot_a: Path
    slot_b: Path
    slot_a_boot_dev: Path
    slot_b_boot_dev: Path


class SlotAB(NamedTuple):
    slot_a: Path
    slot_b: Path


def _run_http_server(addr: str, port: int, *, directory: str) -> None:
    """Serve ``directory`` over HTTP on ``addr:port`` forever.

    Runs in a subprocess via multiprocessing so the server lives outside
    the test event loop and can be killed on teardown.
    """
    import http.server as http_server

    http_server.SimpleHTTPRequestHandler.log_message = lambda *args, **kwargs: None
    handler_class = partial(http_server.SimpleHTTPRequestHandler, directory=directory)
    with http_server.ThreadingHTTPServer((addr, port), handler_class) as httpd:
        httpd.serve_forever()


def _serve_directory(addr: str, port: int, directory: Path) -> Generator[str]:
    """Spawn the HTTP server subprocess and yield its base URL."""
    proc = Process(
        target=_run_http_server,
        args=(addr, port),
        kwargs={"directory": str(directory)},
        daemon=True,
    )
    try:
        proc.start()
        # Give the server a moment to bind before tests start hitting it.
        time.sleep(2)
        logger.info(f"started ota-image server (directory={directory})")
        yield f"http://{addr}:{port}"
    finally:
        proc.kill()
        proc.join()
        logger.info(f"stopped ota-image server (directory={directory})")


@pytest.fixture(scope="session")
def legacy_ota_image_server() -> Generator[str]:
    """Serve `/ota-image` over HTTP on the legacy port and yield the base URL."""
    yield from _serve_directory(
        OTA_IMAGE_SERVER_ADDR, OTA_IMAGE_SERVER_PORT, OTA_IMAGE_DIR
    )


@pytest.fixture(scope="session")
def ota_image_v1_server() -> Generator[str]:
    """Serve `/ota-image_v1` over HTTP on the v1 port and yield the base URL."""
    yield from _serve_directory(
        OTA_IMAGE_V1_SERVER_ADDR, OTA_IMAGE_V1_SERVER_PORT, OTA_IMAGE_V1_DIR
    )


@pytest.fixture
def ab_slots(tmp_path_factory: pytest.TempPathFactory) -> Generator[SlotMeta]:
    """Build the A/B slot layout from the `/ota-image` data dir.

    `slot_a` is the active slot, populated from `/ota-image/data` with
    `var/` renamed to `var_old/` to simulate the diff between the
    current-installed version and the update payload. Symlinks to the
    versioned kernel/initrd are created in `slot_a/boot/` so the boot
    controller's pre-update bookkeeping can resolve them.

    `slot_b` is the standby slot — empty, ready to be populated by the
    updater under test.
    """
    logger.info("creating ab_slots for testing ...")
    slot_a = tmp_path_factory.mktemp("slot_a")
    shutil.copytree(OTA_IMAGE_DIR / "data", slot_a, dirs_exist_ok=True, symlinks=True)
    # Simulate the per-version diff so the updater has work to do.
    shutil.move(str(slot_a / "var"), slot_a / "var_old")

    vmlinuz_symlink = slot_a / "boot" / KERNEL_PREFIX
    initrd_symlink = slot_a / "boot" / INITRD_PREFIX
    try:
        vmlinuz_symlink.symlink_to(f"{KERNEL_PREFIX}-{KERNEL_VERSION}")
        initrd_symlink.symlink_to(f"{INITRD_PREFIX}-{KERNEL_VERSION}")
    except FileExistsError:
        pass

    slot_b = tmp_path_factory.mktemp("slot_b")

    slot_a_boot_dev = tmp_path_factory.mktemp("slot_a_boot")
    slot_a_boot_dir = slot_a_boot_dev / "boot"
    slot_a_boot_dir.mkdir()
    shutil.copytree(OTA_IMAGE_DIR / "data/boot", slot_a_boot_dir, dirs_exist_ok=True)
    slot_b_boot_dev = tmp_path_factory.mktemp("slot_b_boot")
    slot_b_boot_dir = slot_b_boot_dev / "boot"
    slot_b_boot_dir.mkdir()
    (slot_b_boot_dir / "grub").mkdir()

    try:
        yield SlotMeta(
            slot_a=slot_a,
            slot_b=slot_b,
            slot_a_boot_dev=slot_a_boot_dev,
            slot_b_boot_dev=slot_b_boot_dev,
        )
    finally:
        shutil.rmtree(slot_a, ignore_errors=True)
        shutil.rmtree(slot_b, ignore_errors=True)


@pytest.fixture
def ab_slots_for_inplace(tmp_path: Path) -> SlotAB:
    """Simple A/B slots for inplace update mode (create_standby tests)."""
    logger.info("prepare simple a/b slots for inplace update mode ...")
    slot_a = tmp_path / "slot_a"
    slot_b = tmp_path / "slot_b"
    logger.info(f"prepare simple a/b slots for inplace mode: {slot_a=}, {slot_b=}")

    slot_a.mkdir(exist_ok=True, parents=True)
    shutil.copytree(OTA_IMAGE_DATA_DIR, slot_b, symlinks=True)

    # NOTE(20250702): edge condition found on bench test:
    #   fpath of a folder in standby slot becomes symlink in the new image.
    # in newer ubuntu, /sbin becomes a symlink points to /usr/sbin
    sbin = slot_b / "sbin"
    sbin.unlink(missing_ok=True)
    sbin.mkdir(exist_ok=True, parents=True)
    return SlotAB(slot_a, slot_b)


@pytest.fixture
def resource_dir(tmp_path: Path) -> Generator[Path]:
    """Function-scoped resource dir for create_standby delta/update tests."""
    _rd = tmp_path / ".ota-resources"
    logger.info(f"prepare function scrope resource dir: {_rd} ...")
    try:
        _rd.mkdir()
        yield _rd
    finally:
        logger.info(f"cleanup function scrope resource dir: {_rd} ...")
        shutil.rmtree(_rd, ignore_errors=True)


# NOTE: during delta calculation, the ft_resource table will be altered, so
#       this fixture needs to be function scope fixture.
@pytest.fixture
def fst_db_helper(tmp_path_factory: pytest.TempPathFactory) -> FileTableDBHelper:
    """Build the file_table database from the legacy /ota-image CSV fixtures."""
    # Heavy ota_metadata / ota_image_libs imports are kept local to the fixture
    # so importing this conftest stays cheap for tests that don't need them.
    import sqlite3
    from contextlib import closing

    from ota_image_libs.v1.file_table.db import (
        FileTableDBHelper,
        FileTableDirORM,
        FileTableInodeORM,
        FileTableNonRegularORM,
        FileTableRegularORM,
        FileTableResourceORM,
    )

    from ota_metadata.legacy2.csv_parser import (
        parse_dirs_from_csv_file,
        parse_regulars_from_csv_file,
        parse_symlinks_from_csv_file,
    )
    from ota_metadata.legacy2.rs_table import ResourceTableORM

    image_meta_d = tmp_path_factory.mktemp(basename="image-meta")
    ft_dbf = image_meta_d / "file_table.sqlite3"
    rst_dbf = image_meta_d / "resource_table.sqlite3"

    with closing(sqlite3.connect(ft_dbf)) as fst_conn, closing(
        sqlite3.connect(rst_dbf)
    ) as rst_conn:
        ft_regular_orm = FileTableRegularORM(fst_conn)
        ft_regular_orm.orm_bootstrap_db()
        ft_dir_orm = FileTableDirORM(fst_conn)
        ft_dir_orm.orm_bootstrap_db()
        ft_non_regular_orm = FileTableNonRegularORM(fst_conn)
        ft_non_regular_orm.orm_bootstrap_db()
        ft_resource_orm = FileTableResourceORM(fst_conn)
        ft_resource_orm.orm_bootstrap_db()
        ft_inode_orm = FileTableInodeORM(fst_conn)
        ft_inode_orm.orm_bootstrap_db()
        rs_orm = ResourceTableORM(rst_conn)
        rs_orm.orm_bootstrap_db()

        inode_start = 1
        regulars_num, inode_start = parse_regulars_from_csv_file(
            _fpath=REGULARS_TXT,
            _orm=ft_regular_orm,
            _orm_ft_resource=ft_resource_orm,
            _orm_rs=rs_orm,
            _orm_inode=ft_inode_orm,
            inode_start=inode_start,
        )
        dirs_num, inode_start = parse_dirs_from_csv_file(
            DIRS_TXT,
            ft_dir_orm,
            _inode_orm=ft_inode_orm,
            inode_start=inode_start,
        )
        symlinks_num, _ = parse_symlinks_from_csv_file(
            SYMLINKS_TXT,
            ft_non_regular_orm,
            _inode_orm=ft_inode_orm,
            inode_start=inode_start,
        )
        logger.info(
            f"csv parse finished: {dirs_num=}, {symlinks_num=}, {regulars_num=}"
        )
    return FileTableDBHelper(ft_dbf)
