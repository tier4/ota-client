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
import subprocess
import time
from hashlib import sha256
from multiprocessing import Process
from pathlib import Path
from typing import Tuple

import pytest

from otaclient_common import replace_root
from otaclient_common.common import (
    copytree_identical,
    ensure_otaproxy_start,
    subprocess_call,
    subprocess_check_output,
)
from tests.conftest import run_http_server
from tests.utils import compare_dir

logger = logging.getLogger(__name__)

_TEST_FILE_CONTENT = "123456789abcdefgh" * 3000
_TEST_FILE_SHA256 = sha256(_TEST_FILE_CONTENT.encode()).hexdigest()
_TEST_FILE_LENGTH = len(_TEST_FILE_CONTENT.encode())


@pytest.fixture
def file_t(tmp_path: Path) -> Tuple[str, str, int]:
    """A fixture that returns a path to a test file and its sha256."""
    test_f = tmp_path / "test_file"
    test_f.write_text(_TEST_FILE_CONTENT)
    return str(test_f), _TEST_FILE_SHA256, _TEST_FILE_LENGTH


class Test_copytree_identical:
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
        # check result
        compare_dir(self.a_dir, self.b_dir)


class Test_ensure_otaproxy_start:
    DUMMY_SERVER_ADDR, DUMMY_SERVER_PORT = "127.0.0.1", 18888
    DUMMY_SERVER_URL = f"http://{DUMMY_SERVER_ADDR}:{DUMMY_SERVER_PORT}"
    LAUNCH_DELAY = 6

    # for faster testing
    PROBING_INTERVAL = 0.1
    PROBING_CONNECTION_TIMEOUT = 0.1

    @staticmethod
    def _launch_server_helper(addr: str, port: int, launch_delay: int, directory: str):
        time.sleep(launch_delay)
        run_http_server(addr, port, directory=directory)

    @pytest.fixture
    def subprocess_launch_server(self, tmp_path: Path):
        (dummy_webroot := tmp_path / "webroot").mkdir(exist_ok=True)
        _server_p = Process(
            target=self._launch_server_helper,
            args=[
                self.DUMMY_SERVER_ADDR,
                self.DUMMY_SERVER_PORT,
                self.LAUNCH_DELAY,
                str(dummy_webroot),
            ],
        )
        try:
            logger.info(f"wait for {self.LAUNCH_DELAY}s before launching the server")
            _server_p.start()
            yield
        finally:
            _server_p.kill()

    def test_timeout_waiting(self):
        """
        NOTE: we intentionally not use the subprocess_launch_server fixture here
              to let the probing timeout.
        """
        # make testing faster
        probing_timeout = self.LAUNCH_DELAY // 2

        start_time = int(time.time())
        with pytest.raises(ConnectionError):
            ensure_otaproxy_start(
                self.DUMMY_SERVER_URL,
                interval=self.PROBING_INTERVAL,
                connection_timeout=self.PROBING_CONNECTION_TIMEOUT,
                probing_timeout=probing_timeout,
            )
        # probing should cost at least <LAUNCH_DELAY> seconds
        assert int(time.time()) >= start_time + probing_timeout

    def test_probing_delayed_online_server(self, subprocess_launch_server):
        start_time = int(time.time())
        probing_timeout = self.LAUNCH_DELAY * 2

        ensure_otaproxy_start(
            self.DUMMY_SERVER_URL,
            interval=self.PROBING_INTERVAL,
            connection_timeout=self.PROBING_CONNECTION_TIMEOUT,
            probing_timeout=probing_timeout,
        )
        # probing should cost at least <LAUNCH_DELAY> seconds
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

        # test exception supressed
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

        # test exception supressed
        default_value = "test default_value"
        output = subprocess_check_output(
            cmd, default=default_value, raise_exception=False
        )
        assert output == default_value

    def test_subprocess_check_output_succeeded(self):
        cmd = ["cat", str(self.existed_file)]

        output = subprocess_check_output(cmd, raise_exception=True)
        assert output == self.TEST_FILE_CONTENTS


@pytest.mark.parametrize(
    "path, old_root, new_root, expected",
    (
        (
            "/a/canonical/fpath",
            "/",
            "/mnt/standby_mp",
            "/mnt/standby_mp/a/canonical/fpath",
        ),
        (
            "/a/canonical/dpath/",
            "/",
            "/mnt/standby_mp/",
            "/mnt/standby_mp/a/canonical/dpath/",
        ),
    ),
)
def get_replace_root(path, old_root, new_root, expected):
    assert replace_root(path, old_root, new_root) == expected
