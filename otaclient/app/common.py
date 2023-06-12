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
import threading
import requests
import subprocess
import time
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from hashlib import sha256
from pathlib import Path
from queue import Queue
from typing import (
    Any,
    Callable,
    Dict,
    Generator,
    List,
    NamedTuple,
    Optional,
    Set,
    Tuple,
    Union,
    Iterable,
    TypeVar,
    Generic,
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


# ------ RetryTaskMap ------ #

T = TypeVar("T")


class DoneTask(NamedTuple):
    fut: Future
    entry: Any


class RetryTaskMapInterrupted(Exception):
    pass


class _TaskMap(Generic[T]):
    def __init__(
        self,
        executor: ThreadPoolExecutor,
        max_concurrent: int,
        backoff_func: Callable[[int], int],
    ) -> None:
        # task dispatch interval for continues failling
        self.started = False  # can only be started once
        self._backoff_func = backoff_func
        self._executor = executor
        self._shutdown_event = threading.Event()
        self._se = threading.Semaphore(max_concurrent)

        self._total_tasks_count = 0
        self._dispatched_tasks: Set[Future] = set()
        self._failed_tasks: Dict[Future, T] = {}

        self._done_task_counter = itertools.count(start=1)
        self._all_done = threading.Event()
        self._dispatch_done = False

        self._done_que: Queue[DoneTask] = Queue()

    def _done_task_cb(self, item: T, fut: Future):
        """
        Tracking done counting, set all_done event.
        add failed to failed list.
        """
        self._se.release()  # always release se first
        # NOTE: don't change dispatched_tasks if shutdown_event is set
        if self._shutdown_event.is_set():
            return

        self._dispatched_tasks.discard(fut)
        # check if we finish all tasks
        _done_task_num = next(self._done_task_counter)
        if self._dispatch_done and _done_task_num == self._total_tasks_count:
            logger.debug("all done!")
            self._all_done.set()

        if fut.exception():
            self._failed_tasks[fut] = item
        self._done_que.put_nowait(DoneTask(fut, item))

    def _task_dispatcher(self, func: Callable[[T], Any], _iter: Iterable[T]):
        """A dispatcher in a dedicated thread that dispatches
        tasks to threadpool."""
        for item in _iter:
            if self._shutdown_event.is_set():
                return
            self._se.acquire()
            self._total_tasks_count += 1

            fut = self._executor.submit(func, item)
            fut.add_done_callback(partial(self._done_task_cb, item))
            self._dispatched_tasks.add(fut)
        logger.debug(f"dispatcher done: {self._total_tasks_count=}")
        self._dispatch_done = True

    def _done_task_collecter(self) -> Generator[DoneTask, None, None]:
        """A generator for caller to yield done task from."""
        _count = 0
        while not self._shutdown_event.is_set():
            if self._all_done.is_set() and _count == self._total_tasks_count:
                logger.debug("collector done!")
                return

            yield self._done_que.get()
            _count += 1

    def map(self, func: Callable[[T], Any], _iter: Iterable[T]):
        if self.started:
            raise ValueError(f"{self.__class__} inst can only be started once")
        self.started = True

        self._task_dispatcher_fut = self._executor.submit(
            self._task_dispatcher, func, _iter
        )
        self._task_collecter_gen = self._done_task_collecter()
        return self._task_collecter_gen

    def shutdown(self, *, raise_last_exc=False) -> Optional[List[T]]:
        """Set the shutdown event, and cancal/cleanup ongoing tasks."""
        if not self.started or self._shutdown_event.is_set():
            return

        self._shutdown_event.set()
        self._task_collecter_gen.close()
        # wait for dispatch to stop
        self._task_dispatcher_fut.result()

        # cancel all the dispatched tasks
        for fut in self._dispatched_tasks:
            fut.cancel()
        self._dispatched_tasks.clear()

        if not self._failed_tasks:
            return
        try:
            _failed_items = list(self._failed_tasks.values())
            _fut, _item = self._failed_tasks.popitem()
            if _exc := _fut.exception():
                _err_msg = f"{len(_failed_items)=}, last failed {_item=}: {_exc!r}"
                if raise_last_exc:
                    raise RetryTaskMapInterrupted(_err_msg) from _exc
                else:
                    logger.warning(_err_msg)
            return _failed_items
        finally:
            # be careful not to create ref cycle here
            self._failed_tasks.clear()
            _fut, _item, _exc, self = None, None, None, None


class RetryTaskMap:
    def __init__(
        self,
        *,
        backoff_func: Callable[[int], int],
        max_retry: int,
        max_concurrent: int,
        max_workers: Optional[int] = None,
    ) -> None:
        self._running_inst: Optional[_TaskMap] = None
        self._map_gen: Optional[Generator] = None

        self._backoff_func = backoff_func
        self._retry_counter = range(max_retry) if max_retry else itertools.count()
        self._max_concurrent = max_concurrent
        self._max_workers = max_workers
        self._executor = ThreadPoolExecutor(max_workers=self._max_workers)

    def map(
        self, _func: Callable[[T], Any], _iter: Iterable[T]
    ) -> Generator[DoneTask, None, None]:
        for retry_round in self._retry_counter:
            self._running_inst = _inst = _TaskMap(
                self._executor, self._max_concurrent, self._backoff_func
            )
            logger.debug(f"{retry_round=} started")

            yield from _inst.map(_func, _iter)

            # this retry round ends, check overall result
            if _iter := _inst.shutdown(raise_last_exc=False):
                # deref before entering sleep
                self._running_inst, _inst = None, None
                time.sleep(self._backoff_func(retry_round))
            else:  # all tasks finished successfully
                self._running_inst, _inst = None, None
                return
        try:
            raise RetryTaskMapInterrupted(f"exceed try limit: {retry_round}")
        finally:
            # cleanup the defs
            _func, _iter = None, None

    def shutdown(self, *, raise_last_exc: bool):
        try:
            logger.debug("shutdown retry task map")
            if self._running_inst:
                self._running_inst.shutdown(raise_last_exc=raise_last_exc)
            # NOTE: passthrough the exception from underlaying running_inst
        finally:
            self._running_inst = None
            self._executor.shutdown(wait=True)


def create_tmp_fname(prefix="tmp", length=6, sep="_") -> str:
    return f"{prefix}{sep}{os.urandom(length).hex()}"


def ensure_otaproxy_start(
    otaproxy_url: str,
    *,
    interval: float = 1,
    connection_timeout: float = 5,
    probing_timeout: Optional[float] = None,
    warning_interval: int = 3 * 60,  # seconds
):
    """Loop probing <otaproxy_url> until online or exceed <probing_timeout>.

    This function will issue a logging.warning every <warning_interval> seconds.

    Raises:
        A ConnectionError if exceeds <probing_timeout>.
    """
    start_time = int(time.time())
    next_warning = start_time + warning_interval
    probing_timeout = (
        probing_timeout if probing_timeout and probing_timeout >= 0 else float("inf")
    )
    with requests.Session() as session:
        while start_time + probing_timeout > (cur_time := int(time.time())):
            try:
                resp = session.get(otaproxy_url, timeout=connection_timeout)
                resp.close()
                return
            except Exception as e:  # server is not up yet
                if cur_time >= next_warning:
                    logger.warning(
                        f"otaproxy@{otaproxy_url} is not up after {cur_time - start_time} seconds"
                        f"it might be something wrong with this otaproxy: {e!r}"
                    )
                    next_warning = next_warning + warning_interval
                time.sleep(interval)
    raise ConnectionError(
        f"failed to ensure connection to {otaproxy_url} in {probing_timeout=}seconds"
    )
