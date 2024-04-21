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


from __future__ import annotations
import itertools
import logging
import os
import shlex
import shutil
import threading
import requests
import subprocess
import time
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial, lru_cache
from hashlib import sha256
from pathlib import Path
from queue import Queue
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Generator,
    NamedTuple,
    Optional,
    Set,
    Union,
    Iterable,
    TypeVar,
    Generic,
)
from urllib.parse import urljoin

from .configs import config as cfg

from otaclient._utils.linux import (
    map_gid_by_grpnam,
    map_uid_by_pwnam,
    ParsedGroup,
    ParsedPasswd,
)

logger = logging.getLogger(__name__)


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


def _subprocess_run_wrapper(
    cmd: str | list[str],
    *,
    check_output: bool,
    raise_exception: bool,
    default: str = "",
    timeout: Optional[float] = None,
) -> str | None:
    """A wrapper of subprocess.run command."""
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)

    try:
        res = subprocess.run(
            cmd,
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE if check_output else None,
            timeout=timeout,
        )
        if check_output:
            return res.stdout.decode().strip()
    except subprocess.CalledProcessError as e:
        _err_msg = (
            f"command({cmd=}) failed(retcode={e.returncode}: \n"
            f"{e.stderr.decode()}\n"
            f"{e.stdout.decode()}\n)"
        )
        logger.debug(_err_msg)

        if raise_exception:
            raise
        if check_output:
            return default


if TYPE_CHECKING:

    def subprocess_check_output(
        cmd: str | list[str],
        *,
        raise_exception: bool = False,
        default: str = "",
        timeout: Optional[float] = None,
    ) -> str:  # type: ignore
        """Run the command and return its output if possible.
        NOTE: this method will call decode and strip on the raw output.

        Params:
            cmd: string or list of string of command to be called.
            raise_exception: Whether to raise the exception from
                underlaying subprocess.check_output.
            default: when <raise_exception> is True, return this <default>.

        Raises:
            The original CalledProcessError from calling subprocess.check_output if
                the execution failed and <raise_exception> is True.
        """

    def subprocess_call(
        cmd: str | list[str],
        *,
        raise_exception: bool = False,
        timeout: Optional[float] = None,
    ) -> None:
        """Run the <cmd> without checking its output.

        Raises:
            The original CalledProcessError from calling subprocess.check_output if
                the execution failed and <raise_exception> is True.
        """

else:
    subprocess_call = partial(_subprocess_run_wrapper, check_output=False)
    subprocess_check_output = partial(_subprocess_run_wrapper, check_output=True)


def copy_stat(src: Union[Path, str], dst: Union[Path, str]):
    """Copy file/dir permission bits and owner info from src to dst."""
    _stat = Path(src).stat()
    os.chown(dst, _stat.st_uid, _stat.st_gid)
    os.chmod(dst, _stat.st_mode)


def copytree_identical(src: Path, dst: Path):
    """Recursively copy from the src folder to dst folder.

    Source folder MUST be a dir.

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
    if src.is_symlink() or not src.is_dir():
        raise ValueError(f"{src} is not a dir")

    if dst.is_symlink() or not dst.is_dir():
        logger.info(f"{dst=} doesn't exist or not a dir, cleanup and mkdir")
        dst.unlink(missing_ok=True)  # unlink doesn't follow the symlink
        dst.mkdir(mode=src.stat().st_mode, parents=True)

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
        # atomically rename/replace the dst file with the copy
        os.replace(_tmp_file, dst)
        os.sync()
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
        backoff_func: Callable[[int], float],
    ) -> None:
        # task dispatch interval for continues failling
        self.started = False  # can only be started once
        self._backoff_func = backoff_func
        self._executor = executor
        self._shutdown_event = threading.Event()
        self._se = threading.Semaphore(max_concurrent)

        self._total_tasks_count = 0
        self._dispatched_tasks: Set[Future] = set()
        self._failed_tasks: Set[T] = set()
        self._last_failed_fut: Optional[Future] = None

        # NOTE: itertools.count is only thread-safe in CPython with GIL,
        #       as itertools.count is pure C implemented, calling next over
        #       it is atomic in Python level.
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
            self._failed_tasks.add(item)
            self._last_failed_fut = fut
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

    def _done_task_collector(self) -> Generator[DoneTask, None, None]:
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
        self._task_collector_gen = self._done_task_collector()
        return self._task_collector_gen

    def shutdown(self, *, raise_last_exc=False) -> Optional[Set[T]]:
        """Set the shutdown event, and cancal/cleanup ongoing tasks."""
        if not self.started or self._shutdown_event.is_set():
            return

        self._shutdown_event.set()
        self._task_collector_gen.close()
        # wait for dispatch to stop
        self._task_dispatcher_fut.result()

        # cancel all the dispatched tasks
        for fut in self._dispatched_tasks:
            fut.cancel()
        self._dispatched_tasks.clear()

        if not self._failed_tasks:
            return
        try:
            if self._last_failed_fut:
                _exc = self._last_failed_fut.exception()
                _err_msg = f"{len(self._failed_tasks)=}, last failed: {_exc!r}"
                if raise_last_exc:
                    raise RetryTaskMapInterrupted(_err_msg) from _exc
                else:
                    logger.warning(_err_msg)
            return self._failed_tasks.copy()
        finally:
            # be careful not to create ref cycle here
            self._failed_tasks.clear()
            _exc, self = None, None


class RetryTaskMap(Generic[T]):
    def __init__(
        self,
        *,
        backoff_func: Callable[[int], float],
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
        retry_round = 0
        for retry_round in self._retry_counter:
            self._running_inst = _inst = _TaskMap(
                self._executor, self._max_concurrent, self._backoff_func
            )
            logger.debug(f"{retry_round=} started")

            yield from _inst.map(_func, _iter)

            # this retry round ends, check overall result
            if _failed_list := _inst.shutdown(raise_last_exc=False):
                _iter = _failed_list  # feed failed to next round
                # deref before entering sleep
                self._running_inst, _inst = None, None

                logger.warning(f"retry#{retry_round+1}: retry on {len(_failed_list)=}")
                time.sleep(self._backoff_func(retry_round))
            else:  # all tasks finished successfully
                self._running_inst, _inst = None, None
                return
        try:
            raise RetryTaskMapInterrupted(f"exceed try limit: {retry_round}")
        finally:
            # cleanup the defs
            _func, _iter = None, None  # type: ignore

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


#
# ------ persist files handling ------ #
#
class PersistFilesHandler:
    """Preserving files in persist list from <src_root> to <dst_root>.

    Files being copied will have mode bits preserved,
    and uid/gid preserved with mapping as follow:

        src_uid -> src_name -> dst_name -> dst_uid
        src_gid -> src_name -> dst_name -> dst_gid
    """

    def __init__(
        self,
        src_passwd_file: str | Path,
        src_group_file: str | Path,
        dst_passwd_file: str | Path,
        dst_group_file: str | Path,
        *,
        src_root: str | Path,
        dst_root: str | Path,
    ):
        self._uid_mapper = lru_cache()(
            partial(
                self.map_uid_by_pwnam,
                src_db=ParsedPasswd(src_passwd_file),
                dst_db=ParsedPasswd(dst_passwd_file),
            )
        )
        self._gid_mapper = lru_cache()(
            partial(
                self.map_gid_by_grpnam,
                src_db=ParsedGroup(src_group_file),
                dst_db=ParsedGroup(dst_group_file),
            )
        )
        self._src_root = Path(src_root)
        self._dst_root = Path(dst_root)

    @staticmethod
    def map_uid_by_pwnam(
        *, src_db: ParsedPasswd, dst_db: ParsedPasswd, uid: int
    ) -> int:
        _mapped_uid = map_uid_by_pwnam(src_db=src_db, dst_db=dst_db, uid=uid)
        _usern = src_db._by_uid[uid]

        logger.info(f"{_usern=}: mapping src_{uid=} to {_mapped_uid=}")
        return _mapped_uid

    @staticmethod
    def map_gid_by_grpnam(*, src_db: ParsedGroup, dst_db: ParsedGroup, gid: int) -> int:
        _mapped_gid = map_gid_by_grpnam(src_db=src_db, dst_db=dst_db, gid=gid)
        _groupn = src_db._by_gid[gid]

        logger.info(f"{_groupn=}: mapping src_{gid=} to {_mapped_gid=}")
        return _mapped_gid

    def _chown_with_mapping(
        self, _src_stat: os.stat_result, _dst_path: str | Path
    ) -> None:
        _src_uid, _src_gid = _src_stat.st_uid, _src_stat.st_gid
        try:
            _dst_uid = self._uid_mapper(uid=_src_uid)
        except ValueError:
            logger.warning(f"failed to find mapping for {_src_uid=}, keep unchanged")
            _dst_uid = _src_uid

        try:
            _dst_gid = self._gid_mapper(gid=_src_gid)
        except ValueError:
            logger.warning(f"failed to find mapping for {_src_gid=}, keep unchanged")
            _dst_gid = _src_gid
        os.chown(_dst_path, uid=_dst_uid, gid=_dst_gid, follow_symlinks=False)

    @staticmethod
    def _rm_target(_target: Path) -> None:
        """Remove target with proper methods."""
        if _target.is_symlink() or _target.is_file():
            return _target.unlink(missing_ok=True)
        elif _target.is_dir():
            return shutil.rmtree(_target, ignore_errors=True)
        elif _target.exists():
            raise ValueError(
                f"{_target} is not normal file/symlink/dir, failed to remove"
            )

    def _prepare_symlink(self, _src_path: Path, _dst_path: Path) -> None:
        _dst_path.symlink_to(os.readlink(_src_path))
        # NOTE: to get stat from symlink, using os.stat with follow_symlinks=False
        self._chown_with_mapping(os.stat(_src_path, follow_symlinks=False), _dst_path)

    def _prepare_dir(self, _src_path: Path, _dst_path: Path) -> None:
        _dst_path.mkdir(exist_ok=True)

        _src_stat = os.stat(_src_path, follow_symlinks=False)
        os.chmod(_dst_path, _src_stat.st_mode)
        self._chown_with_mapping(_src_stat, _dst_path)

    def _prepare_file(self, _src_path: Path, _dst_path: Path) -> None:
        shutil.copy(_src_path, _dst_path, follow_symlinks=False)

        _src_stat = os.stat(_src_path, follow_symlinks=False)
        os.chmod(_dst_path, _src_stat.st_mode)
        self._chown_with_mapping(_src_stat, _dst_path)

    def _prepare_parent(self, _origin_entry: Path) -> None:
        for _parent in reversed(_origin_entry.parents):
            _src_parent, _dst_parent = (
                self._src_root / _parent,
                self._dst_root / _parent,
            )
            if _dst_parent.is_dir():  # keep the origin parent on dst as it
                continue
            if _dst_parent.is_symlink() or _dst_parent.is_file():
                _dst_parent.unlink(missing_ok=True)
                self._prepare_dir(_src_parent, _dst_parent)
                continue
            if _dst_parent.exists():
                raise ValueError(
                    f"{_dst_parent=} is not a normal file/symlink/dir, cannot cleanup"
                )
            self._prepare_dir(_src_parent, _dst_parent)

    # API

    def preserve_persist_entry(
        self, _persist_entry: str | Path, *, src_missing_ok: bool = True
    ):
        logger.info(f"preserving {_persist_entry}")
        # persist_entry in persists.txt must be rooted at /
        origin_entry = Path(_persist_entry).relative_to("/")
        src_path = self._src_root / origin_entry
        dst_path = self._dst_root / origin_entry

        # ------ src is symlink ------ #
        # NOTE: always check if symlink first as is_file/is_dir/exists all follow_symlinks
        if src_path.is_symlink():
            self._rm_target(dst_path)
            self._prepare_parent(origin_entry)
            self._prepare_symlink(src_path, dst_path)
            return

        # ------ src is file ------ #
        if src_path.is_file():
            self._rm_target(dst_path)
            self._prepare_parent(origin_entry)
            self._prepare_file(src_path, dst_path)
            return

        # ------ src is not regular file/symlink/dir ------ #
        # we only process normal file/symlink/dir
        if src_path.exists() and not src_path.is_dir():
            raise ValueError(f"{src_path=} must be either a file/symlink/dir")

        # ------ src doesn't exist ------ #
        if not src_path.exists():
            _err_msg = f"{src_path=} not found"
            logger.warning(_err_msg)
            if not src_missing_ok:
                raise ValueError(_err_msg)
            return

        # ------ src is dir ------ #
        # dive into src_dir and preserve everything under the src dir
        self._prepare_parent(origin_entry)
        for src_curdir, dnames, fnames in os.walk(src_path, followlinks=False):
            src_cur_dpath = Path(src_curdir)
            dst_cur_dpath = self._dst_root / src_cur_dpath.relative_to(self._src_root)

            # ------ prepare current dir itself ------ #
            self._rm_target(dst_cur_dpath)
            self._prepare_dir(src_cur_dpath, dst_cur_dpath)

            # ------ prepare entries in current dir ------ #
            for _fname in fnames:
                _src_fpath, _dst_fpath = src_cur_dpath / _fname, dst_cur_dpath / _fname
                self._rm_target(_dst_fpath)
                if _src_fpath.is_symlink():
                    self._prepare_symlink(_src_fpath, _dst_fpath)
                    continue
                self._prepare_file(_src_fpath, _dst_fpath)

            # symlinks to dirs also included in dnames, we must handle it
            for _dname in dnames:
                _src_dpath, _dst_dpath = src_cur_dpath / _dname, dst_cur_dpath / _dname
                if _src_dpath.is_symlink():
                    self._rm_target(_dst_dpath)
                    self._prepare_symlink(_src_dpath, _dst_dpath)
