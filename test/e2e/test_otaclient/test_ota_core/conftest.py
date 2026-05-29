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
import time
from functools import partial
from multiprocessing import Process
from pathlib import Path
from typing import Generator, NamedTuple

import pytest
import pytest_mock

from otaclient import ota_core

logger = logging.getLogger(__name__)

# Baked into the test container image; see docker/test_base/Dockerfile.
OTA_IMAGE_DIR = Path("/ota-image")
CERTS_DIR = Path("/certs")
OTA_IMAGE_V1_DIR = Path("/ota-image_v1")
CERTS_OTA_IMAGE_V1_DIR = Path("/certs_ota-image_v1")

KERNEL_PREFIX = "vmlinuz"
INITRD_PREFIX = "initrd.img"

# Local OTA image HTTP servers, fronting the container-baked image dirs.
OTA_IMAGE_SERVER_ADDR = "127.0.0.1"
OTA_IMAGE_SERVER_PORT = 8080
OTA_IMAGE_URL = f"http://{OTA_IMAGE_SERVER_ADDR}:{OTA_IMAGE_SERVER_PORT}"
OTA_IMAGE_V1_SERVER_ADDR = "127.0.0.1"
OTA_IMAGE_V1_SERVER_PORT = 8081
OTA_IMAGE_V1_URL = f"http://{OTA_IMAGE_V1_SERVER_ADDR}:{OTA_IMAGE_V1_SERVER_PORT}"

UPDATE_VERSION = "789.x"

# Signed CloudFront cookies for the OTA image fixture; the signature is
# expired, but the value still needs to round-trip through the downloader
# JSON parser unchanged.
COOKIES_JSON = (
    '{"CloudFront-Policy": "eyJTdGF0ZW1lbnQiOlt7IlJlc291cmNlIjoiaHR0cHM6Ly9maX'
    "Jtd2FyZS1pbWFnZS5jaS53ZWIuYXV0by94Ml9kZXYvOWZjMTA2ZjAtMWJlZC00YzMyLTg5Zm"
    "EtNjUzOWYxZDc1NWJlLyoiLCJDb25kaXRpb24iOnsiRGF0ZUxlc3NUaGFuIjp7IkFXUzpFcG"
    '9jaFRpbWUiOjE3NTEzMzI0OTN9fX1dfQ__", "CloudFront-Signature": "iGhqJjjLnN'
    "UNuF8zdy0wJVUVXABsIPdCgy0rrnLHXT8MANJtcFydyf0LcxKzbIR9654ek0NmkYgeUakv5U"
    "96pacGWfNgVO0z-5BxZiZjaph9PLFqX0kanmSUGTk2vdQm0o67qg~hiTBh0~OzdXK12J~Uuc"
    "Obr4xgm7TxH08QFbVxRzvSkFVVqNhd2JqFp70ihgS~AGtn8ZmOUsHRNIfqiLkz4HdvqgvnpJ"
    "TmvEyFYeaooSEw1usJ3svbUzhJ3WB25UiShUymGtcG5QHVcApB-jH40hfW8qd42l06OQb6J2"
    'E6XMEw710PczGWeZf3WbV7nmSE-2C5J7pZXZadePXi8w__", "CloudFront-Key-Pair-Id'
    '": "K2HIO3GARJTNVV"}'
)


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


OTA_UPDATER_MODULE = ota_core._updater.__name__


@pytest.fixture(autouse=True, scope="module")
def mock_certs_dir(module_mocker: pytest_mock.MockerFixture) -> None:
    """Repoint CERT_DPATH at the certs baked into the test container image."""
    from otaclient.configs.cfg import cfg as _cfg

    module_mocker.patch.object(_cfg, "CERT_DPATH", str(CERTS_DIR))


@pytest.fixture(autouse=True, scope="module")
def mock_fstrim(module_mocker: pytest_mock.MockerFixture) -> None:
    """Block real `fstrim` invocations from leaking out during slot apply."""
    module_mocker.patch(
        f"{OTA_UPDATER_MODULE}.fstrim_at_subprocess",
        module_mocker.MagicMock(),
    )


@pytest.fixture(autouse=True)
def mock_ensure_umount(mocker: pytest_mock.MockerFixture) -> None:
    """`OTAUpdater.execute()`'s finally-block umounts the session workdir.

    The workdir is a plain `tmp_path` subtree (never mounted), so the
    real call would just shell out to `findmnt` + `umount` for no reason
    on every test invocation.
    """
    mocker.patch(f"{OTA_UPDATER_MODULE}.ensure_umount")
