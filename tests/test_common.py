from pathlib import Path
import subprocess
import time
from typing import Tuple
from hashlib import sha256
import pytest
import random

from app.common import (
    file_sha256,
    read_from_file,
    subprocess_call,
    subprocess_check_output,
    verify_file,
    write_to_file_sync,
)

_TEST_FILE_CONTENT = "123456789abcdefgh" * 3000
_TEST_FILE_SHA256 = sha256(_TEST_FILE_CONTENT.encode()).hexdigest()
_TEST_FILE_LENGTH = len(_TEST_FILE_CONTENT)


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


class TestSimpleTasksTracker:
    def worker_thread1(self):
        pass

    def worker_thread2(self):
        pass

    def workload(self, idx: int, *, total: int) -> int:
        time.sleep(total - random.randint(0, idx))
        return idx

    def test_main(self):
        pass
