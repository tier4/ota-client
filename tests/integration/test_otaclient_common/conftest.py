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
"""Downloader fixtures for the otaclient_common integration suite."""

from __future__ import annotations

import itertools
import logging
import os
import random
import socket
import stat
import time
from dataclasses import dataclass
from functools import partial
from hashlib import sha256
from multiprocessing import Process
from pathlib import Path
from typing import Callable, NamedTuple
from urllib.parse import quote

import pytest
import zstandard

from otaclient_common.common import urljoin_ensure_base
from otaclient_common.persist_file_handling import PersistFilesHandler

logger = logging.getLogger(__name__)

# A modest default keeps the suite quick on CI while still exercising
# the threaded download pool.  Individual tests can opt to use a slice.
TEST_FILES_NUM = 256
# NOTE(20240702): exercise URL escaping
TEST_SPECIAL_FILE_NAME = r"path;adf.ae?qu.er\y=str#fragファイルement"
TEST_FILE_SIZE_LOWER_BOUND = 128
TEST_FILE_SIZE_UPPER_BOUND = 4096
COMPRESSION_FILE_SIZE_LOWER_BOUND = 1024


class FileInfo(NamedTuple):
    file_name: str
    url: str
    size: int
    sha256digest: str
    compresson_alg: str | None


def _find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def http_server_endpoint() -> tuple[str, int]:
    """Allocate a free port for the test HTTP server.

    Each test module gets its own port to avoid collisions when the
    runner re-imports modules for parametrized collections.
    """
    return "127.0.0.1", _find_free_port()


@pytest.fixture(scope="module")
def setup_test_data(
    tmp_path_factory: pytest.TempPathFactory,
    http_server_endpoint: tuple[str, int],
) -> tuple[Path, list[FileInfo]]:
    """Generate the on-disk corpus the test HTTP server will serve.

    Each blob:
      * has random content of length [128, 4096) bytes,
      * is zstd-compressed if size >= 1KiB,
      * is named by its index (with one extra blob using
        TEST_SPECIAL_FILE_NAME to exercise URL escaping).
    The returned ``FileInfo`` records hold the *uncompressed* size and
    digest, mirroring how OTA image metadata describes the resource.
    """
    host, port = http_server_endpoint
    test_data_dir = tmp_path_factory.mktemp("downloader_test_data")
    zstd_cctx = zstandard.ZstdCompressor()
    base_url = f"http://{host}:{port}/"

    file_info_list: list[FileInfo] = []
    for fname in map(
        str, itertools.chain(range(TEST_FILES_NUM), [TEST_SPECIAL_FILE_NAME])
    ):
        file = test_data_dir / fname
        # see legacy parser for URL-escape behaviour:
        #   tests have backwards-compat coverage for the old OTA image format
        file_url = urljoin_ensure_base(base_url, quote(fname))

        file_size = random.randint(
            TEST_FILE_SIZE_LOWER_BOUND, TEST_FILE_SIZE_UPPER_BOUND
        )
        file_content = os.urandom(file_size)
        # size and sha256 reflect the uncompressed payload, not the
        # bytes actually stored on disk
        file_sha256digest = sha256(file_content).hexdigest()

        zstd_compressed = file_size >= COMPRESSION_FILE_SIZE_LOWER_BOUND
        if zstd_compressed:
            file_content = zstd_cctx.compress(file_content)

        file.write_bytes(file_content)
        file_info_list.append(
            FileInfo(
                file_name=str(fname),
                url=file_url,
                size=file_size,
                sha256digest=file_sha256digest,
                compresson_alg="zstd" if zstd_compressed else None,
            )
        )
    os.sync()

    random.shuffle(file_info_list)
    logger.info("finished generating %d test blobs", len(file_info_list))
    return test_data_dir, file_info_list


@pytest.fixture(scope="module")
def run_http_server_subprocess(
    setup_test_data: tuple[Path, list[FileInfo]],
    http_server_endpoint: tuple[str, int],
):
    """Serve the generated corpus over HTTP for the lifetime of the module.

    NOTE: not ``autouse`` — this conftest is shared with sibling modules
    (``test_common``, ``test_io``) that have nothing to do with the
    downloader corpus, so they must not pay for spinning it up. The
    downloader suite opts in via a module-scoped autouse fixture in
    ``test_downloader.py``.
    """
    test_data_dir, _ = setup_test_data
    host, port = http_server_endpoint

    def _run():
        import http.server as http_server

        def _silent_logger(*args, **kwargs):
            """Mute the SimpleHTTPRequestHandler default access log."""

        http_server.SimpleHTTPRequestHandler.log_message = _silent_logger

        handler_class = partial(
            http_server.SimpleHTTPRequestHandler,
            directory=str(test_data_dir),
        )
        with http_server.ThreadingHTTPServer((host, port), handler_class) as httpd:
            httpd.serve_forever()

    server_p = Process(target=_run, daemon=True)
    try:
        server_p.start()
        # wait for the server to bind; poll the port instead of a fixed sleep
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((host, port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError(f"test HTTP server did not come up on {host}:{port}")
        logger.info("test HTTP server up on %s:%s for %s", host, port, test_data_dir)
        yield
    finally:
        logger.info("shutting down test HTTP server")
        server_p.kill()
        server_p.join(timeout=5)


#
# ------------ PersistFilesHandler integration fixtures ------------ #
#
# Mapping convention encoded in the passwd/group files below:
#   dst_uid = src_uid + 100   (root: 0 -> 100, alice: 1 -> 101, ...)
#   dst_gid = src_gid + 200   (root: 0 -> 200, agroup: 10 -> 210, ...)
#
# Anything outside the listed users/groups is unmappable: the handler must
# keep the original src uid/gid in that case.

_USERS = [
    ("root", 0),
    ("alice", 1),
    ("bob", 2),
    ("carol", 3),
    ("dave", 4),
    ("eve", 5),
    ("frank", 6),
]
# group names match user names so a passwd entry's gid resolves through the
# group file.
_GROUPS = [
    ("root", 0),
    ("agroup", 10),
    ("bgroup", 20),
    ("cgroup", 30),
    ("dgroup", 40),
    ("egroup", 50),
    ("fgroup", 60),
]
UID_OFFSET = 100
GID_OFFSET = 200

# Unmappable ids — not present in any passwd/group entry. Handler must keep
# the original src uid/gid for these.
UNMAPPABLE_UID = 99001
UNMAPPABLE_GID = 99002


def _passwd_line(name: str, uid: int) -> str:
    return f"{name}:x:{uid}:{uid}::/nonexistent:/usr/sbin/nologin\n"


def _group_line(name: str, gid: int) -> str:
    return f"{name}:x:{gid}:\n"


def src_uid(name: str) -> int:
    return dict(_USERS)[name]


def src_gid(name: str) -> int:
    return dict(_GROUPS)[name]


def dst_uid(name: str) -> int:
    return src_uid(name) + UID_OFFSET


def dst_gid(name: str) -> int:
    return src_gid(name) + GID_OFFSET


@dataclass(frozen=True)
class PwGrpFiles:
    src_passwd: Path
    dst_passwd: Path
    src_group: Path
    dst_group: Path


@pytest.fixture
def pwgrp_files(tmp_path: Path) -> PwGrpFiles:
    """Write minimal passwd/group files for src and dst sides.

    The files live under tmp_path/etc/ and use the offsets defined above.
    """
    etc = tmp_path / "etc"
    etc.mkdir()

    src_passwd = etc / "src_passwd"
    dst_passwd = etc / "dst_passwd"
    src_group = etc / "src_group"
    dst_group = etc / "dst_group"

    src_passwd.write_text("".join(_passwd_line(n, u) for n, u in _USERS))
    dst_passwd.write_text("".join(_passwd_line(n, u + UID_OFFSET) for n, u in _USERS))
    src_group.write_text("".join(_group_line(n, g) for n, g in _GROUPS))
    dst_group.write_text("".join(_group_line(n, g + GID_OFFSET) for n, g in _GROUPS))

    return PwGrpFiles(src_passwd, dst_passwd, src_group, dst_group)


@dataclass(frozen=True)
class Roots:
    src: Path
    dst: Path


@pytest.fixture
def roots(tmp_path: Path) -> Roots:
    """Empty src and dst rootfs directories under tmp_path."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    return Roots(src, dst)


@pytest.fixture
def make_handler(
    pwgrp_files: PwGrpFiles,
) -> Callable[[Path, Path], PersistFilesHandler]:
    """Factory that builds a PersistFilesHandler bound to the given roots."""

    def _factory(src_root: Path, dst_root: Path) -> PersistFilesHandler:
        return PersistFilesHandler(
            src_passwd_file=pwgrp_files.src_passwd,
            src_group_file=pwgrp_files.src_group,
            dst_passwd_file=pwgrp_files.dst_passwd,
            dst_group_file=pwgrp_files.dst_group,
            src_root=src_root,
            dst_root=dst_root,
        )

    return _factory


@pytest.fixture
def handler(
    roots: Roots,
    make_handler: Callable[[Path, Path], PersistFilesHandler],
) -> PersistFilesHandler:
    return make_handler(roots.src, roots.dst)


def stat_uid_gid_mode(path: Path) -> tuple[int, int, int]:
    """Return (uid, gid, mode bits) for path, without following symlinks."""
    st = os.stat(path, follow_symlinks=False)
    return st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)


def assert_uid_gid_mode(path: Path, *, uid: int, gid: int, mode: int) -> None:
    actual = stat_uid_gid_mode(path)
    assert actual == (uid, gid, mode), (
        f"{path}: expected (uid={uid}, gid={gid}, mode={oct(mode)}), "
        f"got (uid={actual[0]}, gid={actual[1]}, mode={oct(actual[2])})"
    )


def persist_entry_for(path: Path, src_root: Path) -> str:
    """Build a canonical persist entry (rooted at /) for path under src_root."""
    return "/" + str(path.relative_to(src_root))


def make_owned_file(path: Path, content: str, *, uid: int, gid: int, mode: int) -> Path:
    path.write_text(content)
    os.chmod(path, mode)
    os.chown(path, uid, gid, follow_symlinks=False)
    return path


def make_owned_dir(path: Path, *, uid: int, gid: int, mode: int) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)
    os.chown(path, uid, gid, follow_symlinks=False)
    return path


def make_owned_symlink(link: Path, target: str, *, uid: int, gid: int) -> Path:
    link.symlink_to(target)
    os.chown(link, uid, gid, follow_symlinks=False)
    return link
