r"""Utils that shared between modules are listed here."""
import itertools
import os
import shlex
import shutil
import enum
import subprocess
import time
from concurrent.futures import Future
from hashlib import sha256
from pathlib import Path
from threading import Event, Semaphore
from typing import Callable, Optional, Union

from app.log_util import get_logger
from app.configs import config as cfg

logger = get_logger(__name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL))


class OTAFileCacheControl(enum.Enum):
    """Custom header for ota file caching control policies.

    format:
        Ota-File-Cache-Control: <directive>
    directives:
        retry_cache: indicates that ota_proxy should clear cache entry for <URL>
            and retry caching
        no_cache: indicates that ota_proxy should not use cache for <URL>
        use_cache: implicitly applied default value, conflicts with no_cache directive
            no need(and no effect) to add this directive into the list

    NOTE: using retry_cache and no_cache together will not work as expected,
        only no_cache will be respected, already cached file will not be deleted as retry_cache indicates.
    """

    use_cache = "use_cache"
    no_cache = "no_cache"
    retry_caching = "retry_caching"

    header = "Ota-File-Cache-Control"
    header_lower = "ota-file-cache-control"

    @classmethod
    def parse_to_value_set(cls, input: str) -> "set[str]":
        return set(input.split(","))

    @classmethod
    def parse_to_enum_set(cls, input: str) -> "set[OTAFileCacheControl]":
        _policies_set = cls.parse_to_value_set(input)
        res = set()
        for p in _policies_set:
            res.add(OTAFileCacheControl[p])

        return res

    @classmethod
    def add_to(cls, target: str, input: "OTAFileCacheControl") -> str:
        _policies_set = cls.parse_to_value_set(target)
        _policies_set.add(input.value)
        return ",".join(_policies_set)


# file verification
def file_sha256(filename: Union[Path, str]) -> str:
    with open(filename, "rb") as f:
        m = sha256()
        while True:
            d = f.read(cfg.LOCAL_CHUNK_SIZE)
            if len(d) == 0:
                break
            m.update(d)
        return m.hexdigest()


def verify_file(fpath: Path, fhash: str, fsize: int) -> bool:
    if not fpath.is_file() or (fsize and fpath.stat().st_size != fsize):
        return False
    return file_sha256(fpath) == fhash


# handled file read/write
def read_from_file(path: Path, *, missing_ok=True) -> str:
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        if missing_ok:
            return ""

        raise


def write_to_file(path: Path, input: str):
    path.write_text(input)


# wrapped subprocess call
def subprocess_call(cmd: str, *, raise_exception=False):
    """

    Raises:
        a ValueError containing information about the failure.
    """
    try:
        # NOTE: we need to check the stderr and stdout when error occurs,
        # so use subprocess.run here instead of subprocess.check_call
        subprocess.run(
            shlex.split(cmd),
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        msg = f"command({cmd=}) failed({e.returncode=}, {e.stderr=}, {e.stdout=})"
        logger.debug(msg)
        if raise_exception:
            raise


def subprocess_check_output(cmd: str, *, raise_exception=False, default="") -> str:
    """
    Raises:
        a ValueError containing information about the failure.
    """
    try:
        return subprocess.check_output(shlex.split(cmd)).decode().strip()
    except subprocess.CalledProcessError as e:
        msg = f"command({cmd=}) failed({e.returncode=}, {e.stderr=}, {e.stdout=})"
        logger.debug(msg)
        if raise_exception:
            raise
        return default


def copy_stat(src: Union[Path, str], dst: Union[Path, str]):
    """Copy file/dir permission bits and owner info from src to dst."""
    _stat = Path(src).stat()
    os.chown(dst, _stat.st_uid, _stat.st_gid)
    os.chmod(dst, _stat.st_mode)


def dst_symlink_as_src(src: Path, dst: Path):
    """Make the dst a symlink link as src."""
    if dst.is_symlink() or dst.is_file():
        dst.unlink(missing_ok=True)

    dst.symlink_to(os.readlink(src))


def copytree_identical(src: Path, dst: Path):
    """Recursively copy from the src folder to dst folder.

    This function populate files/dirs from the src to the dst,
    and make sure the dst is identical to the src.

    By updating the dst folder in-place, we can prevent the case
    that the copy is interrupted and the dst is not yet fully populated.

    This function is different from shutil.copytree as follow:
    1. it covers the case that the same path points to different
        file type, in this case, the dst path will be clean and
        new file/dir will be populated as the src.
    2. it deals with the same symlinks by checking the link target,
        re-generate the symlink if the dst symlink is not the same
        as the src.
    3. it will remove files that not presented in the src, and
        unconditionally override files with same path, ensuring
        that the dst will be identical with the src.
    """
    if not dst.is_dir():
        raise FileNotFoundError(f"{dst} is not found or not a dir")

    # phase1: populate files to the dst
    for cur_dir, _, files in os.walk(src, topdown=True, followlinks=False):
        _cur_dir = Path(cur_dir)
        _cur_dir_on_dst = dst / _cur_dir.relative_to(src)

        # cover the edge case that dst is not a dir.
        if not _cur_dir_on_dst.is_dir():
            _cur_dir_on_dst.unlink(missing_ok=True)
            _cur_dir_on_dst.mkdir(parents=True)
            copy_stat(_cur_dir, _cur_dir_on_dst)

        # populate files
        for fname in files:
            _src_f = _cur_dir / fname
            _dst_f = _cur_dir_on_dst / fname

            # src and dst type mismatch, dst is a folder
            if _dst_f.is_dir():
                shutil.rmtree(_dst_f, ignore_errors=True)

            # check symlink, re-link if needed
            if _src_f.is_symlink():
                dst_symlink_as_src(_src_f, _dst_f)
                continue

            # finally populate the new file or override the dst file
            shutil.copy(_src_f, _dst_f, follow_symlinks=False)
            copy_stat(_src_f, _dst_f)

    # phase2: remove unused files in the dst
    for cur_dir, dirs, files in os.walk(dst, topdown=True, followlinks=False):
        _cur_dir_on_dst = Path(cur_dir)
        _cur_dir_on_src = src / _cur_dir_on_dst.relative_to(dst)

        # remove unused dir
        if not _cur_dir_on_src.is_dir():
            shutil.rmtree(_cur_dir_on_dst, ignore_errors=True)
            dirs.clear()  # stop iterate the subfolders of this dir
            continue

        for fname in files:
            _src_f = _cur_dir_on_src / fname
            if not (_src_f.is_file() or _src_f.is_symlink()):
                (_cur_dir_on_dst / fname).unlink(missing_ok=True)


class SimpleTasksTracker:
    def __init__(
        self, *, max_concurrent: int, title: str = "simple_tasks_tracker"
    ) -> None:
        self.title = title
        self._wait_interval = cfg.STATS_COLLECT_INTERVAL
        self.last_error = None
        self._se = Semaphore(max_concurrent)

        self._interrupted = Event()
        self._register_finished = False

        self._in_counter = itertools.count()
        self._done_counter = itertools.count()
        self._in_num = 0
        self._done_num = 0

    def add_task(self):
        if self._interrupted.is_set() or self._register_finished:
            return

        self._se.acquire()
        self._in_num = next(self._in_counter)

    def task_collect_finished(self):
        self._register_finished = True

    def done_callback(self, fut: Future) -> None:
        try:
            self._se.release()
            fut.result()
            self._done_num = next(self._done_counter)
        except Exception as e:
            self.last_error = e
            self._interrupted.set()

    def wait(
        self,
        extra_wait_cb: Optional[Callable] = None,
        *,
        raise_exception: bool = True,
    ):
        while not self._register_finished or self._done_num < self._in_num:
            if self._interrupted.is_set():
                logger.error(f"{self.title} interrupted, abort")
                break

            time.sleep(self._wait_interval)

        # if extra_wait_cb presents, also wait for it
        if callable(extra_wait_cb):
            extra_wait_cb()

        if raise_exception and self.last_error:
            raise self.last_error
