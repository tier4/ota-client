import os
import subprocess
import time
import pytest
import random
import logging
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from typing import Set, Tuple

from app.common import (
    OTAFileCacheControl,
    SimpleTasksTracker,
    copytree_identical,
    file_sha256,
    re_symlink_atomic,
    read_from_file,
    subprocess_call,
    subprocess_check_output,
    verify_file,
    write_to_file_sync,
)

logger = logging.getLogger(__name__)

_TEST_FILE_CONTENT = "123456789abcdefgh" * 3000
_TEST_FILE_SHA256 = sha256(_TEST_FILE_CONTENT.encode()).hexdigest()
_TEST_FILE_LENGTH = len(_TEST_FILE_CONTENT)


@pytest.fixture
def file_t(tmp_path: Path) -> Tuple[str, str, int]:
    """A fixture that returns a path to a test file and its sha256."""
    test_f = tmp_path / "test_file"
    test_f.write_text(_TEST_FILE_CONTENT)
    return str(test_f), _TEST_FILE_SHA256, _TEST_FILE_LENGTH


@pytest.mark.parametrize(
    "_input, expected",
    (
        (
            "use_cache,retry_caching",
            {OTAFileCacheControl.use_cache, OTAFileCacheControl.retry_caching},
        ),
        ("no_cache", {OTAFileCacheControl.no_cache}),
    ),
)
def test_parse_to_enum_set(_input: str, expected: Set[OTAFileCacheControl]):
    assert OTAFileCacheControl.parse_to_enum_set(_input) == expected


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

    assert read_from_file(_path) == _TEST_FILE_CONTENT
    assert read_from_file("/non-existed", missing_ok=True, default="") == ""
    assert read_from_file("/non-existed", missing_ok=True, default="abc") == "abc"
    with pytest.raises(FileNotFoundError):
        read_from_file("/non-existed", missing_ok=False)


def test_write_to_file_sync(tmp_path: Path):
    _path = tmp_path / "write_to_file"
    write_to_file_sync(_path, _TEST_FILE_CONTENT)
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
            dir_1(dir)/
                dir_1_file_1(file)
            symlink_1(symlink->dir_1)
        b_dir/ # target
            file_1(dir)/
            dir_1(dir)/
                dir_1_file_1(symlink->x3)
                x3(file)
            symlink_1(symlink->x4)
            x4(dir)/
            x5(symlink->x4)
            x6(file)
            x7(symlink->123123)
        """
        # populate a_dir
        a_dir = tmp_path / "a_dir"
        a_dir.mkdir()
        file_1 = a_dir / "file_1"
        file_1.write_text("file_1")
        dir_1 = a_dir / "dir_1"
        dir_1.mkdir()
        dir_1_file_1 = dir_1 / "dir_1_file_1"
        dir_1_file_1.write_text("dir_1_file_1")
        symlink_1 = a_dir / "symlink_1"
        symlink_1.symlink_to("dir_1")

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
        x7.symlink_to("123123")

        # register
        self.a_dir = a_dir
        self.b_dir = b_dir

    def compare_dir(self):
        _a_glob = set(map(lambda x: x.relative_to(self.a_dir), self.a_dir.glob("**/*")))
        _b_glob = set(map(lambda x: x.relative_to(self.b_dir), self.b_dir.glob("**/*")))
        assert _a_glob == _b_glob  # first check paths are identical

        # then check each file/folder of the path
        # NOTE/TODO: stats is not checked
        for _path in _a_glob:
            _a_path = self.a_dir / _path
            _b_path = self.b_dir / _path
            if _a_path.is_symlink():
                assert _b_path.is_symlink() and os.readlink(_a_path) == os.readlink(
                    _b_path
                )
            elif _a_path.is_dir():
                assert _b_path.is_dir()

            elif _a_path.is_file():
                assert _b_path.is_file() and file_sha256(_a_path) == file_sha256(
                    _b_path
                )
            else:
                assert False, f"unexpected file type for {_path}"

    def test_copytree_identical(self):
        copytree_identical(self.a_dir, self.b_dir)
        # check result
        self.compare_dir()


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


class TestSimpleTasksTracker:
    WAIT_CONST = 100_000_000
    TASKS_COUNT = 3000
    MAX_CONCURRENT = 60

    def workload(self, idx: int, *, total: int) -> int:
        time.sleep((total - random.randint(0, idx)) / self.WAIT_CONST)
        return idx

    def interrupt_workload(self, idx: int):
        raise ValueError(f"interrupted at {idx}")

    def extra_wait(self):
        time.sleep(1)
        logger.info("extra wait exits")

    def test_successfully_completed(self):
        _task_tracker = SimpleTasksTracker(max_concurrent=self.MAX_CONCURRENT)
        with ThreadPoolExecutor() as pool:
            for i in range(self.TASKS_COUNT):
                fut = pool.submit(self.workload, i, total=self.TASKS_COUNT)
                _task_tracker.add_task(fut)
                fut.add_done_callback(_task_tracker.done_callback)
            _task_tracker.task_collect_finished()
            logger.info("tasks dispatching completed")
            _task_tracker.wait(self.extra_wait)

    def test_interrupted(self):
        _task_tracker = SimpleTasksTracker(max_concurrent=self.MAX_CONCURRENT)
        with ThreadPoolExecutor() as pool, pytest.raises(ValueError):
            for i in range(self.TASKS_COUNT):
                if i == self.TASKS_COUNT // 3 * 2:
                    logger.info(f"interrupt workload called at {i}")
                    fut = pool.submit(self.interrupt_workload, i)
                else:
                    fut = pool.submit(self.workload, i, total=self.TASKS_COUNT)
                _task_tracker.add_task(fut)
                fut.add_done_callback(_task_tracker.done_callback)
            _task_tracker.task_collect_finished()
            logger.info("tasks dispatching completed")
            _task_tracker.wait(self.extra_wait)
