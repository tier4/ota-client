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
from concurrent.futures import Future, Executor
from functools import partial
from hashlib import sha256
from pathlib import Path
from queue import Queue, Empty
from threading import Semaphore, Event, Lock
from typing import (
    Callable,
    Generator,
    Optional,
    Set,
    Tuple,
    Union,
    Iterable,
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


_T, _RES = TypeVar("_T"), TypeVar("_RES")


class _RetryTaskMapErr(Exception):
    pass


class _RetryTaskMapStatus(enum.Enum):
    INIT = "INIT"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    SHUTDOWNED = "SHUTDOWNED"


class _ThreadSafeGenWrapper:
    def __init__(self, _gen: Generator, *, kick_start: bool) -> None:
        self._gen = _gen
        self._lock = Lock()
        if kick_start:
            self.send(None)

    def close(self):
        self._gen.close()

    def send(self, __value: Any) -> Any:
        try:
            with self._lock:
                return self._gen.send(__value)
        except StopIteration:
            pass  # ignore on closed gen

    def throw(self, __exp: Exception):
        try:
            with self._lock:
                self._gen.throw(__exp)
        except Exception as __exp:  # break ref cycle
            raise __exp


class Task(Generic[_T, _RES]):
    def __init__(self, _func: Callable[[_T], _RES], _entry: _T) -> None:
        self._func = _func
        self._entry = _entry

    def __call__(
        self,
    ) -> Tuple[Optional[Exception], _T, Optional[_RES]]:
        try:
            return None, self._entry, self._func(self._entry)
        except Exception as e:
            return e, self._entry, None
        finally:
            self = None  # resolve ref cycle


class RetryTaskMap(Generic[_T, _RES]):
    """A map like class that try its best to finish all the tasks.

    Inst of this class is initialized with a <_func>, and <_iter> like built-in map,
    It will try to finish all the tasks, if some of the tasks failed, it
    will retry on those failed tasks in the next round. Reapting the process
    untill all the element in the input <_iter> is successfully processed,
    or max retries exceeded the limitation.

    NOTE: the inst of this class can only be used once.
    NOTE: the API of this class is NOT thread-safe.
    NOTE: RetryTaskMap will consume the input iterable and turn it into a list.

    Args:
        _func: function to be applied to every element of the input _iter.
        _iter: a iterable contains elements to be processed.
        executor: executor for tasks dispatching.
        backoff_max: max wait time between each retry round.
        backoff_factor: factor for calculating wait time.
        max_failed: <= 0 means no max_failed.
        max_retry: <=0 means no max_retry.
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
        self.max_concurrent = max_concurrent
        self._func = _func
        self._executor = executor
        self._max_retry_iter = range(max_retry) if max_retry > 0 else itertools.count()
        # life cycle
        self._status_lock = Lock()
        self._status = _RetryTaskMapStatus.INIT

        # tasks handling
        self._collector_done_this_round = Event()
        self._yield_que: Queue[Future] = Queue()
        self._task_collector_gen: _ThreadSafeGenWrapper = None  # type: ignore
        self._task_dispatcher_thread: Future = None  # type: ignore
        self._last_error: Exception = None  # type: ignore

        # values specific to each retry
        # start from the consuming the input _iter
        self._tasks_list: List[_T] = list(_iter)
        self._futs: Set[Future] = set()
        self._failed_tasks: List[_T] = []
        self._last_failed_count = 0
        self._backoff_wait_f = partial(
            wait_with_backoff, _backoff_factor=backoff_factor, _backoff_max=backoff_max
        )

    def _task_collector(self) -> Generator[None, Future, None]:
        try:
            while self._status is _RetryTaskMapStatus.RUNNING:
                _tasks_counter, _tasks_done = itertools.count(start=1), 0
                while self._status is _RetryTaskMapStatus.RUNNING:
                    _fut = yield
                    try:
                        _exp, _entry, _ = _fut.result()
                        if _exp is not None:
                            self._failed_tasks.append(_entry)
                            self._last_error = _exp
                        # give the fut to the upper caller
                        self._yield_que.put_nowait(_fut)
                    except Exception:  # ignored as not caused by task itself
                        pass
                    finally:
                        _tasks_done = next(_tasks_counter)

                    if _tasks_done == len(self._tasks_list):
                        self._collector_done_this_round.set()
                        break
        finally:
            # always set the flag to prevent dispatcher dead locked
            self._collector_done_this_round.set()

    def _task_dispatcher(self):
        _backoff_retry_count = 0
        for _total_retry_count in self._max_retry_iter:
            _se = Semaphore(self.max_concurrent)  # each retry round has its own se

            def _done_cb(_fut, /):
                _se.release()  # make sure the se is released first
                if self._status is not _RetryTaskMapStatus.RUNNING:
                    return
                self._futs.discard(_fut)
                self._task_collector_gen.send(_fut)

            for _entry in self._tasks_list:
                if self._status is not _RetryTaskMapStatus.RUNNING:
                    return
                _se.acquire(blocking=True)
                _fut = self._executor.submit(Task(self._func, _entry))
                self._futs.add(_fut)
                _fut.add_done_callback(_done_cb)
            self._collector_done_this_round.wait()

            if len(self._failed_tasks) != 0:
                _backoff_retry_count += 1
                logger.error(
                    f"{self.title} failed to finished, {len(self._failed_tasks)=}, {self._last_failed_count=}, {_total_retry_count=}"
                )
                # reset backoff retry count if this retry round
                # at least successfully processed some of the tasks
                if len(self._failed_tasks) < self._last_failed_count:
                    _backoff_retry_count = 0
                # cleanup to prepare for the next retry
                self._last_failed_count = len(self._failed_tasks)
                self._last_error = None  # type: ignore
                self._tasks_list, self._failed_tasks = self._failed_tasks, []
                self._futs.clear()
                self._collector_done_this_round.clear()

                self._backoff_wait_f(_backoff_retry_count)
            else:
                with self._status_lock:
                    if self._status is _RetryTaskMapStatus.RUNNING:
                        self._status = _RetryTaskMapStatus.SUCCEEDED
                return
        # shutdown on exceed retry limit
        with self._status_lock:
            self._status = _RetryTaskMapStatus.SHUTDOWNED

    def _shutdown(self):
        logger.debug(f"shutdown {self.title}...")
        self._task_collector_gen.close()
        self._task_dispatcher_thread.result()
        # cleanup
        for _fut in self._futs:
            _fut.cancel()
        self._futs.clear()
        self._tasks_list.clear()
        self._failed_tasks.clear()

        # NOTE: be very careful here not to create cycle reference
        #       by removing the ref to self in finally statement
        if self._status is _RetryTaskMapStatus.SHUTDOWNED and self._last_error:
            try:
                logger.error(f"{self.title} terminated: {self._last_error=!r}")
                raise _RetryTaskMapErr(f"{self._last_error!r}") from self._last_error
            finally:
                self = None  # resolve cycle reference

    # API

    def shutdown(self) -> bool:
        """Set the internal status to SHUTDOWNED to trigger shutdown process."""
        with self._status_lock:
            if self._status is not _RetryTaskMapStatus.RUNNING:
                return False
            self._status = _RetryTaskMapStatus.SHUTDOWNED
            return True

    def execute(self) -> Iterable[Tuple[Optional[Exception], _T, _RES]]:
        if self._status is not _RetryTaskMapStatus.INIT:
            raise ValueError("call on already closed/started RetryTaskMap, abort")
        self._status = _RetryTaskMapStatus.RUNNING
        self._task_dispatcher_thread = self._executor.submit(self._task_dispatcher)
        # kick start collector generator
        self._task_collector_gen = _ThreadSafeGenWrapper(
            self._task_collector(), kick_start=True
        )

        # return a generator to make sure the yielding happens
        # after the first task has been dispatched
        def _gen():
            while self._status is _RetryTaskMapStatus.RUNNING or (
                self._status is _RetryTaskMapStatus.SUCCEEDED
                and not self._yield_que.empty()
            ):
                try:
                    _fut = self._yield_que.get(block=True, timeout=1)
                    yield _fut.result()
                except Empty:
                    pass

            # NOTE: caller directly controls the gen, so we trigger
            # the actual shutdown here
            self._shutdown()  # shutdown on finish

        return _gen()
