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
import asyncio
import itertools
import os
import shlex
import shutil
import enum
import socket
import subprocess
import time
from concurrent.futures import Future, ThreadPoolExecutor, CancelledError, TimeoutError
from functools import partial
from hashlib import sha256
from threading import Lock, Event, Semaphore
from pathlib import Path
from queue import Queue
from typing import (
    Any,
    Callable,
    Generator,
    NamedTuple,
    Optional,
    Tuple,
    Union,
    Iterable,
    TypeVar,
    Generic,
)
from urllib.parse import urljoin, urlsplit

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


# ------ RetryTaskMap ------ #

_T = TypeVar("_T")
_ROUND_DONE = object()


class FutureWrapper(NamedTuple):
    entry: Any
    fut: Future


class TaskResult(NamedTuple):
    is_successful: bool
    entry: Any
    fut: Future


class _DispatcherCollectorSync:
    def __init__(self) -> None:
        self.tasks_num = 0
        self._task_counter = itertools.count(start=1)
        self._dispatcher_done = Event()
        self._collector_done = Event()

    def dispatch_task(self):
        self.tasks_num = next(self._task_counter)

    def dispatcher_done(self):
        self._dispatcher_done.set()

    def collector_should_exit(self, processed_tasks_num: int) -> bool:
        if self._collector_done.is_set():
            return True
        if self._dispatcher_done.is_set():
            # if dispacher is done, but tasks_num is still 0,
            # it means that dispatcher doesn't dispatch any tasks,
            # collector should exit right now.
            if (
                collector_should_exit := not self.tasks_num
                or processed_tasks_num == self.tasks_num
            ):
                self._collector_done.set()
            return collector_should_exit
        return False

    def wait(self):
        self._dispatcher_done.wait()
        self._collector_done.wait()


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


class RetryTaskMapInterrupted(Exception):
    pass


class RetryTaskMap(Generic[_T]):
    def __init__(
        self,
        *,
        max_concurrent: int,
        executor: ThreadPoolExecutor,
        retry_interval_f: Callable[[int], None],
        title: str = "",
        max_retry: Optional[int] = None,
    ) -> None:
        """Init the RetryTaskMap with configurations and executor instance.

        Params:
            max_concurrent: max number of loaded tasks in the thread pool.
            executor: an instance of ThreadPoolExecutor used to execute tasks,
                NOTE that the pool must has at least 2 workers.
            retry_interval_f: a callable that take retry rounds num as input,
                will be called between retry rounds.
            title: name of this RetryTaskMap instance, will be written in exception message.
            max_retry: max retry rounds before RetryTaskMap breaks out and raise
                RetryTaskMapInterrupted.
        """
        self._executor = executor
        self._shutdowned = Event()

        self._max_retry = range(max_retry) if max_retry else itertools.count()
        self._sync: Optional[_DispatcherCollectorSync] = None
        self._last_failed_exc: Optional[BaseException] = None

        self.title = title
        self.retry_interval_f = retry_interval_f
        self.max_concurrent = max_concurrent

    def _round_task_collector(
        self, que: "Queue[FutureWrapper]", sync: _DispatcherCollectorSync
    ) -> Generator[None, FutureWrapper, None]:
        finished_tasks_count = 0
        while not sync.collector_should_exit(finished_tasks_count):
            finished_tasks_count += 1
            wrapped_fut = yield
            if not self._shutdowned.is_set():
                que.put_nowait(wrapped_fut)
            del wrapped_fut  # release ref
        que.put_nowait(_ROUND_DONE)  # type: ignore

    def _round_task_dispatcher(
        self,
        _func: Callable[[_T], Any],
        _iter: Iterable[_T],
        *,
        que: "Queue[FutureWrapper]",
        sync: _DispatcherCollectorSync,
    ):
        # start the collector
        collector_gen = _ThreadSafeGenWrapper(
            self._round_task_collector(que, sync), kick_start=True
        )
        que = None  # type: ignore ,release ref
        max_concurrent_se = Semaphore(self.max_concurrent)

        def _task_cb(_fut, /, entry):
            collector_gen.send(FutureWrapper(entry, _fut))
            max_concurrent_se.release()  # NOTE: must release after send

        # NOTE: the already dispatched tasks will keep in queue
        #       and being executed even shutdown is set.
        for entry in _iter:
            if self._shutdowned.is_set():
                break
            max_concurrent_se.acquire(blocking=True)
            fut = self._executor.submit(_func, entry)
            sync.dispatch_task()  # NOTE: must count before add callback
            fut.add_done_callback(partial(_task_cb, entry=entry))
        sync.dispatcher_done()

    def _raise_exc(self, msg: str = ""):
        if self._last_failed_exc:
            try:
                raise RetryTaskMapInterrupted(
                    f"<RetryTaskMap: {self.title}> failed({self._last_failed_exc=}): {msg}",
                ) from self._last_failed_exc
            finally:  # be careful not to create ref cycle
                self._last_failed_exc = None
                self = None
        else:
            raise RetryTaskMapInterrupted(
                f"<RetryTaskMap: {self.title}> interrupted: {msg}"
            )

    def _execute(
        self, _func: Callable[[_T], Any], _iter: Iterable[_T], /
    ) -> Generator[Tuple[int, TaskResult], None, None]:
        failed_entries, last_failed_count = [], 0
        retry_round, keep_failing_retry = 0, 0
        for retry_round in self._max_retry:
            sync = _DispatcherCollectorSync()
            que: "Queue[FutureWrapper]" = Queue()
            self._executor.submit(
                self._round_task_dispatcher, _func, _iter, que=que, sync=sync
            )
            self._sync = sync

            while (fut_wrapper := que.get(block=True)) is not _ROUND_DONE:
                # logger.error(f"get collected {_get=}")
                entry, fut = fut_wrapper
                try:  # special treatment to cancelled/timeout future
                    _exc = fut.exception()
                    is_successful = _exc is None
                except (CancelledError, TimeoutError):
                    is_successful = False
                    _exc = None

                if not is_successful:
                    failed_entries.append(entry)
                    if _exc:
                        self._last_failed_exc = _exc

                yield retry_round, TaskResult(is_successful, entry, fut)
                # do not hold uneccessary refs while blocking
                del fut_wrapper, entry, fut, _exc
            # cleanup on this retry round finished
            self._sync = None

            if failed_entries:
                logger.error(
                    f"{self.title} failed to finished, {len(failed_entries)=}, {last_failed_count=}, {retry_round=}"
                )
                # no more succeeded tasks since last retry round
                if not len(failed_entries) < last_failed_count:
                    keep_failing_retry += 1
                else:  # reset failing counter if any task succeeded in this round
                    keep_failing_retry = 0

                last_failed_count = len(failed_entries)
                self.retry_interval_f(keep_failing_retry)
                _iter, failed_entries = failed_entries, []
            else:  # all tasks finished without exception
                return

        try:
            self._raise_exc(f"exceed max retry: {retry_round=}")
        finally:
            self = None  # break ref cycle

    def shutdown(self):
        """Shutdown the current running RetryTaskMap instance.

        Raises:
            RetryTaskMapInterrupted, if self._last_failed_exc is valid
                exception, raise from it, otherwise raise plain
                RetryTaskMapInterrupted.
        """
        logger.debug(f"shutdown <RetryTaskMap: {self.title=}>")
        self._shutdowned.set()
        self._task_executor.close()  # force close to release resources
        # wait for current running session finished
        if self._sync:
            self._sync.wait()

        try:
            self._raise_exc("shutdown by callers")
        finally:
            self = None  # break ref cycle

    def map(
        self, _func: Callable[[_T], Any], _iter: Iterable[_T], /
    ) -> Generator[Tuple[int, TaskResult], None, None]:
        """Returns an iterator that applies <_func> to every entry in <_iter>.

        Yields:
            A tuple of current retry round, and an instance of TaskResult.
            TaskResult is a namedtuple consists of the following:
            - a bool indicates whether this task finished without exception.
            - the entry being processed in this task.
            - the Future instance related to this task.

        Raises:
            RetryTaskMapInterrupted if exceeds <max_retry>.
        """
        self._task_executor = self._execute(_func, _iter)
        return self._task_executor


def create_tmp_fname(prefix="tmp", length=6, sep="_") -> str:
    return f"{prefix}{sep}{os.urandom(length).hex()}"


async def ensure_port_open_async(
    host: str, port: int, *, interval: float, timeout: Optional[float] = None
):
    start_time = int(time.time())
    timeout = timeout if timeout and timeout >= 0 else float("inf")
    loop = asyncio.get_running_loop()
    while start_time + timeout > int(time.time()):
        try:
            transport, _ = await loop.create_connection(
                lambda: asyncio.Protocol(), host, port
            )
            transport.abort()
            return
        except Exception:
            pass
        await asyncio.sleep(interval)
    raise ConnectionError(
        f"failed to ensure connection to {host}:{port} in {timeout=}seconds"
    )


def ensure_port_open(
    host: str, port: int, *, interval: float, timeout: Optional[float] = None
):
    start_time = int(time.time())
    timeout = timeout if timeout and timeout >= 0 else float("inf")
    while start_time + timeout > int(time.time()):
        try:
            s = socket.create_connection((host, port))
            s.close()
            return
        except ConnectionError:
            pass
        time.sleep(interval)
    raise ConnectionError(
        f"failed to ensure connection to {host}:{port} in {timeout=}seconds"
    )


def ensure_http_server_open(
    url: str, *, interval: float, timeout: Optional[float] = None
):
    split_url = urlsplit(url)
    assert (host := split_url.hostname)
    if not (port := split_url.port):
        if split_url.scheme == "https":
            port = 443
        elif split_url.scheme == "http":
            port = 80
        else:
            raise ValueError(f"cannot determine port for {url=}")
    ensure_port_open(host, port, interval=interval, timeout=timeout)


async def ensure_http_server_open_async(
    url: str, *, interval: float, timeout: Optional[float] = None
):
    split_url = urlsplit(url)
    assert (host := split_url.hostname)
    if not (port := split_url.port):
        if split_url.scheme == "https":
            port = 443
        elif split_url.scheme == "http":
            port = 80
        else:
            raise ValueError(f"cannot determine port for {url=}")
    await ensure_port_open_async(host, port, interval=interval, timeout=timeout)
