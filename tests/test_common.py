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


import os
import subprocess
import time
import pytest
import random
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from hashlib import sha256
from multiprocessing import Process
from pathlib import Path
from typing import Tuple

from otaclient.app.common import (
    RetryTaskMap,
    RetryTaskMapInterrupted,
    copytree_identical,
    ensure_otaproxy_start,
    file_sha256,
    get_backoff,
    re_symlink_atomic,
    read_str_from_file,
    subprocess_call,
    subprocess_check_output,
    verify_file,
    wait_with_backoff,
    write_str_to_file_sync,
)
from tests.utils import compare_dir
from tests.conftest import cfg, run_http_server

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


def test_file_sha256(file_t: Tuple[str, str, int]):
    _path, _sha256, _ = file_t
    assert file_sha256(_path) == _sha256


def test_verify_file(tmp_path: Path, file_t: Tuple[str, str, int]):
    _path, _sha256, _size = file_t
    assert verify_file(Path(_path), _sha256, _size)
    assert verify_file(Path(_path), _sha256, None)
    assert not verify_file(Path(_path), _sha256, 123)
    assert not verify_file(Path(_path), sha256().hexdigest(), 123)

    # test over symlink file, verify_file should return False on symlink
    _symlink = tmp_path / "test_file_symlink"
    _symlink.symlink_to(_path)
    assert not verify_file(_symlink, _sha256, None)


def test_read_from_file(file_t: Tuple[str, str, int]):
    _path, _, _ = file_t
    # append some empty lines to the test_file
    _append = "    \n      \n"
    with open(_path, "a") as f:
        f.write(_append)

    assert read_str_from_file(_path) == _TEST_FILE_CONTENT
    assert read_str_from_file("/non-existed", missing_ok=True, default="") == ""
    assert read_str_from_file("/non-existed", missing_ok=True, default="abc") == "abc"
    with pytest.raises(FileNotFoundError):
        read_str_from_file("/non-existed", missing_ok=False)


def test_write_to_file_sync(tmp_path: Path):
    _path = tmp_path / "write_to_file"
    write_str_to_file_sync(_path, _TEST_FILE_CONTENT)
    assert _path.read_text() == _TEST_FILE_CONTENT


def test_subprocess_call():
    with pytest.raises(subprocess.CalledProcessError) as e:
        subprocess_call("ls /non-existed", raise_exception=True)
    _origin_e = e.value
    assert _origin_e.returncode == 2
    assert (
        _origin_e.stderr.decode().strip()
        == "ls: cannot access '/non-existed': No such file or directory"
    )

    subprocess_call("ls /non-existed", raise_exception=False)


def test_subprocess_check_output(file_t: Tuple[str, str, int]):
    _path, _, _ = file_t
    with pytest.raises(subprocess.CalledProcessError) as e:
        subprocess_check_output("cat /non-existed", raise_exception=True)
    _origin_e = e.value
    assert _origin_e.returncode == 1
    assert (
        _origin_e.stderr.decode().strip()
        == "cat: /non-existed: No such file or directory"
    )

    assert (
        subprocess_check_output(
            "cat /non-existed", raise_exception=False, default="abc"
        )
        == "abc"
    )
    assert subprocess_check_output(f"cat {_path}") == _TEST_FILE_CONTENT


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


class Test_re_symlink_atomic:
    def test_symlink_to_file(self, tmp_path: Path):
        _symlink = tmp_path / "_symlink"
        _target = tmp_path / "_target"
        _target.write_text("_target")

        # test1: src symlink doesn't exist
        re_symlink_atomic(_symlink, _target)
        assert _symlink.is_symlink() and os.readlink(_symlink) == str(_target)
        # test2: src symlink is a symlink that points to correct target
        re_symlink_atomic(_symlink, _target)
        assert _symlink.is_symlink() and os.readlink(_symlink) == str(_target)
        # test3: src symlink is a symlink that points to wrong target
        _symlink.unlink(missing_ok=True)
        _symlink.symlink_to("/non-existed")
        re_symlink_atomic(_symlink, _target)
        assert _symlink.is_symlink() and os.readlink(_symlink) == str(_target)
        # test4: src is a file
        _symlink.unlink(missing_ok=True)
        _symlink.write_text("123123123")
        re_symlink_atomic(_symlink, _target)
        assert _symlink.is_symlink() and os.readlink(_symlink) == str(_target)

    def test_symlink_to_directory(self, tmp_path: Path):
        _symlink = tmp_path / "_symlink"
        _target = tmp_path / "_target"
        _target.mkdir()

        # test1: src symlink doesn't exist
        re_symlink_atomic(_symlink, _target)
        assert _symlink.is_symlink() and os.readlink(_symlink) == str(_target)
        # test2: src symlink is a symlink that points to correct target
        re_symlink_atomic(_symlink, _target)
        assert _symlink.is_symlink() and os.readlink(_symlink) == str(_target)
        # test3: src symlink is a symlink that points to wrong target
        _symlink.unlink(missing_ok=True)
        _symlink.symlink_to("/non-existed")
        re_symlink_atomic(_symlink, _target)
        assert _symlink.is_symlink() and os.readlink(_symlink) == str(_target)
        # test4: src is a file
        _symlink.unlink(missing_ok=True)
        _symlink.write_text("123123123")
        re_symlink_atomic(_symlink, _target)
        assert _symlink.is_symlink() and os.readlink(_symlink) == str(_target)


class _RetryTaskMapTestErr(Exception):
    ...


class TestRetryTaskMap:
    WAIT_CONST = 100_000_000
    TASKS_COUNT = 2000
    MAX_CONCURRENT = 128
    DOWNLOAD_GROUP_NO_SUCCESS_RETRY_TIMEOUT = 6  # seconds
    MAX_WAIT_BEFORE_SUCCESS = 10

    @pytest.fixture(autouse=True)
    def setup(self):
        self._start_time = time.time()
        self._success_wait_dict = {
            idx: random.randint(0, self.MAX_WAIT_BEFORE_SUCCESS)
            for idx in range(self.TASKS_COUNT)
        }
        self._succeeded_tasks = [False for _ in range(self.TASKS_COUNT)]

    def workload_aways_failed(self, idx: int) -> int:
        time.sleep((self.TASKS_COUNT - random.randint(0, idx)) / self.WAIT_CONST)
        raise _RetryTaskMapTestErr

    def workload_failed_and_then_succeed(self, idx: int) -> int:
        time.sleep((self.TASKS_COUNT - random.randint(0, idx)) / self.WAIT_CONST)
        if time.time() > self._start_time + self._success_wait_dict[idx]:
            self._succeeded_tasks[idx] = True
            return idx
        raise _RetryTaskMapTestErr

    def workload_succeed(self, idx: int) -> int:
        time.sleep((self.TASKS_COUNT - random.randint(0, idx)) / self.WAIT_CONST)
        self._succeeded_tasks[idx] = True
        return idx

    def test_retry_keep_failing_timeout(self):
        _keep_failing_timer = time.time()
        with pytest.raises(RetryTaskMapInterrupted):
            _mapper = RetryTaskMap(
                backoff_func=partial(get_backoff, factor=0.1, _max=1),
                max_concurrent=self.MAX_CONCURRENT,
                max_retry=0,  # we are testing keep failing timeout here
            )
            for done_task in _mapper.map(
                self.workload_aways_failed, range(self.TASKS_COUNT)
            ):
                if not done_task.fut.exception():
                    # reset the failing timer on one succeeded task
                    _keep_failing_timer = time.time()
                    continue
                if (
                    time.time() - _keep_failing_timer
                    > self.DOWNLOAD_GROUP_NO_SUCCESS_RETRY_TIMEOUT
                ):
                    logger.error(
                        f"RetryTaskMap successfully failed after keep failing in {self.DOWNLOAD_GROUP_NO_SUCCESS_RETRY_TIMEOUT}s"
                    )
                    _mapper.shutdown(raise_last_exc=True)

    def test_retry_exceed_retry_limit(self):
        with pytest.raises(RetryTaskMapInterrupted):
            _mapper = RetryTaskMap(
                backoff_func=partial(get_backoff, factor=0.1, _max=1),
                max_concurrent=self.MAX_CONCURRENT,
                max_retry=3,
            )
            for _ in _mapper.map(self.workload_aways_failed, range(self.TASKS_COUNT)):
                pass

    def test_retry_finally_succeeded(self):
        _keep_failing_timer = time.time()

        _mapper = RetryTaskMap(
            backoff_func=partial(get_backoff, factor=0.1, _max=1),
            max_concurrent=self.MAX_CONCURRENT,
            max_retry=0,  # we are testing keep failing timeout here
        )
        for done_task in _mapper.map(
            self.workload_failed_and_then_succeed, range(self.TASKS_COUNT)
        ):
            # task successfully finished
            if not done_task.fut.exception():
                # reset the failing timer on one succeeded task
                _keep_failing_timer = time.time()
                continue

            if (
                time.time() - _keep_failing_timer
                > self.DOWNLOAD_GROUP_NO_SUCCESS_RETRY_TIMEOUT
            ):
                _mapper.shutdown(raise_last_exc=True)
        assert all(self._succeeded_tasks)

    def test_succeeded_in_one_try(self):
        _keep_failing_timer = time.time()
        _mapper = RetryTaskMap(
            backoff_func=partial(get_backoff, factor=0.1, _max=1),
            max_concurrent=self.MAX_CONCURRENT,
            max_retry=0,  # we are testing keep failing timeout here
        )
        for done_task in _mapper.map(self.workload_succeed, range(self.TASKS_COUNT)):
            # task successfully finished
            if not done_task.fut.exception():
                # reset the failing timer on one succeeded task
                _keep_failing_timer = time.time()
                continue

            if (
                time.time() - _keep_failing_timer
                > self.DOWNLOAD_GROUP_NO_SUCCESS_RETRY_TIMEOUT
            ):
                _mapper.shutdown(raise_last_exc=True)
        assert all(self._succeeded_tasks)


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
