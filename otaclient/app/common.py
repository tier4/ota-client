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


r"""Utils that shared between modules are listed here."""
import itertools
import os
import shlex
import shutil
import enum
import subprocess
import time
from concurrent.futures import (
    CancelledError,
    Future,
    Executor,
    wait,
)
from functools import partial
from hashlib import sha256
from pathlib import Path
from queue import Queue, Empty
from threading import Event, Semaphore
from typing import (
    Callable,
    Optional,
    Set,
    Tuple,
    Union,
    Iterable,
    Iterator,
    Any,
    TypeVar,
    Generic,
    List,
)
from urllib.parse import urljoin

from .log_setting import get_logger
from .configs import config as cfg

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


def get_backoff(n: int, factor: float, _max: float) -> float:
    return min(_max, factor * (2 ** (n - 1)))


def wait_with_backoff(_retry_cnt: int, *, _backoff_factor: float, _backoff_max: float):
    time.sleep(
        get_backoff(
            _retry_cnt,
            _backoff_factor,
            _backoff_max,
        )
    )


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


def verify_file(fpath: Path, fhash: str, fsize: Optional[int]) -> bool:
    if (
        fpath.is_symlink()
        or (not fpath.is_file())
        or (fsize is not None and fpath.stat().st_size != fsize)
    ):
        return False
    return file_sha256(fpath) == fhash


# handled file read/write
def read_str_from_file(path: Union[Path, str], *, missing_ok=True, default="") -> str:
    """
    Params:
        missing_ok: if set to False, FileNotFoundError will be raised to upper
        default: the default value to return when missing_ok=True and file not found
    """
    try:
        return Path(path).read_text().strip()
    except FileNotFoundError:
        if missing_ok:
            return default

        raise


def write_str_to_file(path: Path, input: str):
    path.write_text(input)


def write_str_to_file_sync(path: Union[Path, str], input: str):
    with open(path, "w") as f:
        f.write(input)
        f.flush()
        os.fsync(f.fileno())


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
        return (
            subprocess.check_output(shlex.split(cmd), stderr=subprocess.PIPE)
            .decode()
            .strip()
        )
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

    NOTE: is_file/is_dir also returns True if it is a symlink and
    the link target is_file/is_dir
    """
    if dst.is_symlink() or not dst.is_dir():
        raise FileNotFoundError(f"{dst} is not found or not a dir")

    # phase1: populate files to the dst
    for cur_dir, dirs, files in os.walk(src, topdown=True, followlinks=False):
        _cur_dir = Path(cur_dir)
        _cur_dir_on_dst = dst / _cur_dir.relative_to(src)

        # NOTE(20220803): os.walk now lists symlinks pointed to dir
        # in the <dirs> tuple, we have to handle this behavior
        for _dir in dirs:
            _src_dir = _cur_dir / _dir
            _dst_dir = _cur_dir_on_dst / _dir
            if _src_dir.is_symlink():  # this "dir" is a symlink to a dir
                if (not _dst_dir.is_symlink()) and _dst_dir.is_dir():
                    # if dst is a dir, remove it
                    shutil.rmtree(_dst_dir, ignore_errors=True)
                else:  # dst is symlink or file
                    _dst_dir.unlink()
                _dst_dir.symlink_to(os.readlink(_src_dir))

        # cover the edge case that dst is not a dir.
        if _cur_dir_on_dst.is_symlink() or not _cur_dir_on_dst.is_dir():
            _cur_dir_on_dst.unlink(missing_ok=True)
            _cur_dir_on_dst.mkdir(parents=True)
            copy_stat(_cur_dir, _cur_dir_on_dst)

        # populate files
        for fname in files:
            _src_f = _cur_dir / fname
            _dst_f = _cur_dir_on_dst / fname

            # prepare dst
            #   src is file but dst is a folder
            #   delete the dst in advance
            if (not _dst_f.is_symlink()) and _dst_f.is_dir():
                # if dst is a dir, remove it
                shutil.rmtree(_dst_f, ignore_errors=True)
            else:
                # dst is symlink or file
                _dst_f.unlink(missing_ok=True)

            # copy/symlink dst as src
            #   if src is symlink, check symlink, re-link if needed
            if _src_f.is_symlink():
                _dst_f.symlink_to(os.readlink(_src_f))
            else:
                # copy/override src to dst
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

        # NOTE(20220803): os.walk now lists symlinks pointed to dir
        # in the <dirs> tuple, we have to handle this behavior
        for _dir in dirs:
            _src_dir = _cur_dir_on_src / _dir
            _dst_dir = _cur_dir_on_dst / _dir
            if (not _src_dir.is_symlink()) and _dst_dir.is_symlink():
                _dst_dir.unlink()

        for fname in files:
            _src_f = _cur_dir_on_src / fname
            if not (_src_f.is_symlink() or _src_f.is_file()):
                (_cur_dir_on_dst / fname).unlink(missing_ok=True)


def re_symlink_atomic(src: Path, target: Union[Path, str]):
    """Make the <src> a symlink to <target> atomically.

    If the src is already existed as a file/symlink,
    the src will be replaced by the newly created link unconditionally.

    NOTE: os.rename is atomic when src and dst are on
    the same filesystem under linux.
    NOTE 2: src should not exist or exist as file/symlink.
    """
    if not (src.is_symlink() and str(os.readlink(src)) == str(target)):
        tmp_link = Path(src).parent / f"tmp_link_{os.urandom(6).hex()}"
        try:
            tmp_link.symlink_to(target)
            os.rename(tmp_link, src)  # unconditionally override
        except Exception:
            tmp_link.unlink(missing_ok=True)
            raise


def replace_atomic(src: Union[str, Path], dst: Union[str, Path]):
    """Atomically replace dst file with src file.

    NOTE: atomic is ensured by os.rename/os.replace under the same filesystem.
    """
    src, dst = Path(src), Path(dst)
    if not src.is_file():
        raise ValueError(f"{src=} is not a regular file or not exist")

    _tmp_file = dst.parent / f".tmp_{os.urandom(6).hex()}"
    try:
        # prepare a copy of src file under dst's parent folder
        shutil.copy(src, _tmp_file, follow_symlinks=True)
        os.sync()
        # atomically rename/replace the dst file with the copy
        os.replace(_tmp_file, dst)
    except Exception:
        _tmp_file.unlink(missing_ok=True)
        raise


def urljoin_ensure_base(base: str, url: str):
    """
    NOTE: this method ensure the base_url will be preserved.
          for example:
            base="http://example.com/data", url="path/to/file"
          with urljoin, joined url will be "http://example.com/path/to/file",
          with this func, joined url will be "http://example.com/data/path/to/file"
    """
    return urljoin(f"{base.rstrip('/')}/", url)


_T = TypeVar("_T")


class RetryTaskMap(Generic[_T]):
    """A map like class that try its best to finish all the tasks.

    Inst of this class is initialized with a <_func>, like built-in map,
    it will be applied to each element in later input <_iter>.
    It will try to finish all the tasks, if some of the tasks failed, it
    will retry on those failed tasks in the next round.

    Reapting the process untill all the element in the input <_iter> is
    successfully processed, or max retries exceeded the limitation.

    NOTE: the inst of this class can only be used once.
    NOTE: the inst of this class is NOT thread-safe.

    Args:
        max_failed: <= 0 means no max_failed
        max_retry: <=0 means no max_retry
    """

    def __init__(
        self,
        _func: Callable[[_T], Any],
        _iter: Iterable[_T],
        /,
        *,
        max_concurrent: int,
        executor: Executor,
        title: str = "",
        backoff_max: int = 5,
        backoff_factor: float = 1,
        max_retry: int = 6,
    ) -> None:
        self.title = title
        self._func = _func
        self._executor = executor
        self._se = Semaphore(max_concurrent)
        self._backoff_wait_f = partial(
            wait_with_backoff, _backoff_factor=backoff_factor, _backoff_max=backoff_max
        )
        self._max_retry_iter = range(max_retry) if max_retry > 0 else itertools.count()
        self._shutdowned = False
        self._started = False
        self.last_error = None

        # values specific to each retry
        self._iter: Iterable[_T] = _iter  # start from the input _iter
        self._futs: Set[Future] = set()
        self._done_que: Queue[Future] = Queue()
        self._failed: List[_T] = []
        self._last_failed_count = 0

    def _task_wrapper(self, _entry: _T, /) -> Tuple[Optional[Exception], _T, Any]:
        """Wrap the task and handle the exception.

        Returns:
            A tuple of consists an inst of exception if task failed,
                element that processed by the task function,
                and the result of the function running.
        """
        try:
            return None, _entry, self._func(_entry)
        except Exception as e:
            return e, _entry, None

    def _done_callback(self, _fut: Future, /):
        """Remove the fut from the futs list and release se."""
        try:
            _exp, _entry, _ = _fut.result()
            if _exp:
                self.last_error = _exp
                self._failed.append(_entry)
        except CancelledError:
            pass  # ignored as not caused by task itself
        finally:
            self._futs.discard(_fut)
            self._done_que.put_nowait(_fut)
            self._se.release()

    def _execute(self):
        _backoff_retry_count = 0
        for _total_retry_count in self._max_retry_iter:
            for _entry in self._iter:
                if self._shutdowned:
                    return
                self._se.acquire(blocking=True)
                _fut = self._executor.submit(self._task_wrapper, _entry)
                _fut.add_done_callback(self._done_callback)
                self._futs.add(_fut)
            wait(self._futs)

            if len(self._failed) != 0:
                _backoff_retry_count += 1
                logger.error(
                    f"{self.title} failed to finished, {len(self._failed)=}, {self._last_failed_count=}, {_total_retry_count=}"
                )
                # reset the _retry_count if we managed to make some tasks
                # succeed in this retry
                if len(self._failed) < self._last_failed_count:
                    logger.debug("reset retry_count!")
                    _backoff_retry_count = 0
                # cleanup to prepare for the next retry
                self._last_failed_count = len(self._failed)
                self._futs.clear()
                self._iter, self._failed = self._failed, []
                # sleep before next retry
                self._backoff_wait_f(_backoff_retry_count + 1)
            else:
                self._shutdowned = True
                return
        # failed to finish all tasks under <max_retry_limit>
        if isinstance(self.last_error, Exception):
            raise self.last_error

    def shutdown(self, *, raise_last_exp: bool):
        """Shutdown the current running RetryTaskMap.

        Args:
            raise_last_exp: if True, re-raise the last recorded exp
                if exp is not None.
        """
        logger.debug(f"shutdown the running RetryTaskMap {self.title=}")
        self._shutdowned = True
        # cleanup
        self._failed.clear()
        while self._futs and (_fut := self._futs.pop()):
            _fut.cancel()

        if raise_last_exp and isinstance(self.last_error, Exception):
            raise self.last_error

    def execute(self) -> Iterator[Tuple[Optional[Exception], _T, Any]]:
        if self._shutdowned or self._started:
            logger.debug("call on closed/already started RetryTaskMap, abort")
            return
        self._started = True
        # dispatch the executing thread
        self._executor.submit(self._execute)

        while not self._shutdowned or not self._done_que.empty():
            try:
                yield self._done_que.get(block=True, timeout=1).result()
            except Empty:
                pass
