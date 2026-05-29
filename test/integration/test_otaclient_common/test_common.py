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
"""Integration tests for otaclient_common.common utilities.

These cover three subsystems that each cross an OS boundary:
  * ``copytree_identical`` reconciling two real directory trees (files,
    dirs, symlinks, dangling/circular links) under ``tmp_path``;
  * ``ensure_otaproxy_start`` probing a real HTTP server subprocess that
    only comes online after a delay;
  * ``subprocess_call`` / ``subprocess_check_output`` driving real
    external commands.

The pure-logic ``replace_root`` cases live in the unit tier
(``test/unit/test_otaclient_common/test_common.py``).
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Generator

import pytest

from otaclient_common._io import file_sha256
from otaclient_common.common import (
    copytree_identical,
    ensure_otaproxy_start,
    subprocess_call,
    subprocess_check_output,
)
from test.conftest import launch_http_server_subprocess

logger = logging.getLogger(__name__)


def _find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def compare_dir(left: Path, right: Path) -> None:
    """Assert two directory trees are structurally and byte-for-byte identical.

    Paths, symlink targets and file digests are compared; file stats are
    intentionally not checked.
    """
    _a_glob = set(map(lambda x: x.relative_to(left), left.glob("**/*")))
    _b_glob = set(map(lambda x: x.relative_to(right), right.glob("**/*")))
    if _a_glob != _b_glob:  # first check paths are identical
        raise ValueError(
            f"left and right mismatch, diff: {_a_glob.symmetric_difference(_b_glob)}\n"
            f"{_a_glob=}\n"
            f"{_b_glob=}"
        )

    # then check each file/folder of the path
    for _path in _a_glob:
        _a_path = left / _path
        _b_path = right / _path
        if _a_path.is_symlink():
            if not (
                _b_path.is_symlink() and os.readlink(_a_path) == os.readlink(_b_path)
            ):
                raise ValueError(f"symlink mismatched: {_path}")
        elif _a_path.is_dir():
            if not _b_path.is_dir():
                raise ValueError(f"dir mismatched: {_path}")
        elif _a_path.is_file():
            if not (_b_path.is_file() and file_sha256(_a_path) == file_sha256(_b_path)):
                logger.error(f"{_a_path.read_text()=}, {_b_path.read_text()=}")
                raise ValueError(f"file check failed: {_path}")
        else:
            raise ValueError(f"unspecific file type: {_path}")


class TestCopytreeIdentical:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path):
        """
        a_dir/ # src
            file_1(file)
            file_2(file)
            dir_1(dir)/
                dir_1_file_1(file)
            symlink_1(symlink->dir_1)
            symlink_2(symlink->file_1)
            symlink_3(symlink->/non-existed)
            circular_symlink1(symlink->circular_symlink2)
            circular_symlink2(symlink->circular_symlink1)
        b_dir/ # target
            file_1(dir)/
            dir_1(dir)/
                dir_1_file_1(symlink->x3)
                x3(file)
            symlink_1(symlink->x4)
            x4(dir)/
            x5(symlink->x4)
            x6(file)
            x7(symlink->/non-existed)
            symlink_3(symlink->file_2)
            circular_symlink1(symlink->circular_symlink2)
            circular_symlink2(symlink->circular_symlink1)
        """
        # populate a_dir
        a_dir = tmp_path / "a_dir"
        a_dir.mkdir()
        file_1 = a_dir / "file_1"
        file_1.write_text("file_1")
        file_2 = a_dir / "file_2"
        file_2.write_text("file_2")
        dir_1 = a_dir / "dir_1"
        dir_1.mkdir()
        dir_1_file_1 = dir_1 / "dir_1_file_1"
        dir_1_file_1.write_text("dir_1_file_1")
        symlink_1 = a_dir / "symlink_1"
        symlink_1.symlink_to("dir_1")

        symlink_2 = a_dir / "symlink_2"
        symlink_2.symlink_to("file_1")
        symlink_3 = a_dir / "symlink_3"
        symlink_3.symlink_to("/non-existed")
        circular_symlink1 = a_dir / "circular_symlink1"
        circular_symlink2 = a_dir / "circular_symlink2"
        circular_symlink1.symlink_to("circular_symlink2")
        circular_symlink2.symlink_to("circular_symlink1")

        # populate b_dir
        b_dir = tmp_path / "b_dir"
        b_dir.mkdir()
        file_1 = b_dir / "file_1"
        file_1.mkdir()
        dir_1 = b_dir / "dir_1"
        dir_1.mkdir()
        dir_1_file_1 = dir_1 / "dir_1_file_1"
        dir_1_file_1.symlink_to("x3")
        x3 = dir_1 / "x3"
        x3.write_text("aabbcc")
        symlink_1 = b_dir / "symlink_1"
        symlink_1.symlink_to("x4")
        x4 = b_dir / "x4"
        x4.mkdir()
        x5 = b_dir / "x5"
        x5.symlink_to("x4")
        x6 = b_dir / "x6"
        x6.write_text("123123")
        x7 = b_dir / "x7"
        x7.symlink_to("/non-existed")
        symlink_3 = b_dir / "symlink_3"
        symlink_3.symlink_to("file_2")
        circular_symlink1 = b_dir / "circular_symlink1"
        circular_symlink2 = b_dir / "circular_symlink2"
        circular_symlink1.symlink_to("circular_symlink2")
        circular_symlink2.symlink_to("circular_symlink1")

        # register
        self.a_dir = a_dir
        self.b_dir = b_dir

    def test_copytree_identical(self):
        copytree_identical(self.a_dir, self.b_dir)
        # b_dir should now be an identical copy of a_dir
        compare_dir(self.a_dir, self.b_dir)


class TestEnsureOtaproxyStart:
    SERVER_ADDR = "127.0.0.1"
    LAUNCH_DELAY = 6

    # for faster testing
    PROBING_INTERVAL = 0.1
    PROBING_CONNECTION_TIMEOUT = 0.1

    def test_timeout_waiting(self):
        """No server is ever launched, so probing must time out.

        NOTE: we intentionally do not bring any server up here to let the
              probing loop exhaust its <probing_timeout>.
        """
        otaproxy_url = f"http://{self.SERVER_ADDR}:{_find_free_port()}"
        # make testing faster
        probing_timeout = self.LAUNCH_DELAY // 2

        start_time = int(time.time())
        with pytest.raises(ConnectionError):
            ensure_otaproxy_start(
                otaproxy_url,
                interval=self.PROBING_INTERVAL,
                connection_timeout=self.PROBING_CONNECTION_TIMEOUT,
                probing_timeout=probing_timeout,
            )
        # probing should cost at least <probing_timeout> seconds
        assert int(time.time()) >= start_time + probing_timeout

    @pytest.fixture
    def delayed_otaproxy_server(self, tmp_path: Path) -> Generator[str]:
        """Bring an HTTP server online only after <LAUNCH_DELAY> seconds.

        The port/URL is allocated up front so the test can begin probing
        before the server binds, exercising ensure_otaproxy_start's retry
        loop against a server that comes online late.
        """
        (webroot := tmp_path / "webroot").mkdir(exist_ok=True)
        otaproxy_url = f"http://{self.SERVER_ADDR}:{(port := _find_free_port())}"

        stop = threading.Event()

        def _serve_after_delay() -> None:
            logger.info(f"wait for {self.LAUNCH_DELAY}s before launching the server")
            time.sleep(self.LAUNCH_DELAY)
            with launch_http_server_subprocess(self.SERVER_ADDR, port, webroot):
                stop.wait()

        server_thread = threading.Thread(target=_serve_after_delay, daemon=True)
        server_thread.start()
        try:
            yield otaproxy_url
        finally:
            stop.set()
            server_thread.join(timeout=self.LAUNCH_DELAY + 15)

    def test_probing_delayed_online_server(self, delayed_otaproxy_server: str):
        start_time = int(time.time())
        probing_timeout = self.LAUNCH_DELAY * 2

        ensure_otaproxy_start(
            delayed_otaproxy_server,
            interval=self.PROBING_INTERVAL,
            connection_timeout=self.PROBING_CONNECTION_TIMEOUT,
            probing_timeout=probing_timeout,
        )
        # the server only comes online after <LAUNCH_DELAY> seconds, so
        #   probing must have waited at least that long
        assert int(time.time()) >= start_time + self.LAUNCH_DELAY


class TestSubprocessCall:
    TEST_FILE_CONTENTS = "test file contents"

    @pytest.fixture(autouse=True)
    def setup_test(self, tmp_path: Path):
        test_file = tmp_path / "test_file"
        test_file.write_text(self.TEST_FILE_CONTENTS)
        self.existed_file = test_file
        self.non_existed_file = tmp_path / "non_existed_file"

    def test_subprocess_call_failed(self):
        cmd = ["ls", str(self.non_existed_file)]
        with pytest.raises(subprocess.CalledProcessError) as e:
            subprocess_call(cmd, raise_exception=True)
        origin_exc: subprocess.CalledProcessError = e.value

        assert origin_exc.returncode == 2
        assert (
            origin_exc.stderr.decode().strip()
            == f"ls: cannot access '{self.non_existed_file}': No such file or directory"
        )

        # test exception suppressed
        subprocess_call(cmd, raise_exception=False)

    def test_subprocess_call_succeeded(self):
        cmd = ["ls", str(self.existed_file)]
        subprocess_call(cmd, raise_exception=True)

    def test_subprocess_check_output_failed(self):
        cmd = ["cat", str(self.non_existed_file)]

        with pytest.raises(subprocess.CalledProcessError) as e:
            subprocess_check_output(cmd, raise_exception=True)
        origin_exc: subprocess.CalledProcessError = e.value
        assert origin_exc.returncode == 1
        assert (
            origin_exc.stderr.decode().strip()
            == f"cat: {self.non_existed_file}: No such file or directory"
        )

        # test exception suppressed
        default_value = "test default_value"
        output = subprocess_check_output(
            cmd, default=default_value, raise_exception=False
        )
        assert output == default_value

    def test_subprocess_check_output_succeeded(self):
        cmd = ["cat", str(self.existed_file)]

        output = subprocess_check_output(cmd, raise_exception=True)
        assert output == self.TEST_FILE_CONTENTS
