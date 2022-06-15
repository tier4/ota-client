r"""Common used helpers, classes and functions for different bank creating methods."""
from abc import abstractmethod
import collections
import os
import queue
import shutil
import time
import weakref
from concurrent.futures import (
    FIRST_EXCEPTION,
    Future,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    wait,
)
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Semaphore
from typing import (
    Any,
    Callable,
    ClassVar,
    Generator,
    Iterator,
    List,
    Dict,
    Literal,
    OrderedDict,
    Protocol,
    Tuple,
    Union,
)

from app.common import file_sha256
from app.configs import config as cfg
from app.interface import OTAUpdateStatsCollectorProtocol
from app.ota_metadata import OtaMetadata, RegularInf
from app import log_util
from app.update_stats import OTAUpdateStatsCollector

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class CreateStandbySlotError(Exception):
    pass


class CreateStandbySlotExternalError(CreateStandbySlotError):
    """Error caused by calling external program.

    For ota-client, typically we map this Error as Recoverable.
    """


class CreateStandbySlotInternalError(CreateStandbySlotError):
    """Error caused by internal logic.

    For ota-client, typically we map this Error as Unrecoverable.
    """


@dataclass
class UpdateMeta:
    """Meta info for standby slot creator to update slot."""

    cookies: Dict[str, Any]  # cookies needed for requesting remote ota files
    metadata: OtaMetadata  # meta data for the update request
    url_base: str  # base url of the remote ota image


class StandbySlotCreatorProtocol(Protocol):
    """Protocol that describes bank creating.
    Attrs:
        cookies: authentication cookies used by ota_client to fetch files from the remote ota server.
        metadata: metadata of the requested ota image.
        url_base: base url that ota image located.
        new_root: the root folder of bank to be updated.
        reference_root: the root folder to copy from.
        status_tracker: pass real-time update stats to ota-client.
        status_updator: inform which ota phase now.
    """

    update_meta: UpdateMeta
    stats_tracker: OTAUpdateStatsCollectorProtocol
    update_phase_tracker: Callable

    @abstractmethod
    def create_standby_bank(self):
        ...

    @classmethod
    @abstractmethod
    def should_erase_standby_slot(cls) -> bool:
        """Tell whether standby slot should be erased
        under this standby slot creating mode."""


class _WeakRef:
    pass


class _HardlinkTracker:
    POLLINTERVAL = 0.1

    def __init__(self, first_copy_path: Path, ref: _WeakRef, count: int):
        self._first_copy_ready = Event()
        self._failed = Event()
        # hold <count> refs to ref
        self._ref_holder: List[_WeakRef] = [ref for _ in range(count)]

        self.first_copy_path = first_copy_path

    def writer_done(self):
        self._first_copy_ready.set()

    def writer_on_failed(self):
        self._failed.set()
        self._ref_holder.clear()

    def subscribe(self) -> Path:
        # wait for writer
        while not self._first_copy_ready.is_set():
            if self._failed.is_set():
                raise ValueError(f"writer failed on path={self.first_copy_path}")

            time.sleep(self.POLLINTERVAL)

        try:
            self._ref_holder.pop()
        except IndexError:
            # it won't happen generally as this tracker will be gc
            # after the ref holder holds no more ref.
            pass

        return self.first_copy_path

    def subscribe_no_wait(self) -> Path:
        return self.first_copy_path


class HardlinkRegister:
    def __init__(self):
        self._lock = Lock()
        self._hash_ref_dict: Dict[str, _WeakRef] = weakref.WeakValueDictionary()
        self._ref_tracker_dict: Dict[
            _WeakRef, _HardlinkTracker
        ] = weakref.WeakKeyDictionary()

    def get_tracker(
        self, _identifier: str, path: Path, nlink: int
    ) -> "Tuple[_HardlinkTracker, bool]":
        """Get a hardlink tracker from the register.

        Args:
            _identifier: a string that can identify a group of hardlink file.
            path: path that the caller wants to save file to.
            nlink: number of hard links in this hardlink group.

        Returns:
            A hardlink tracker and a bool to indicates whether the caller is the writer or not.
        """
        with self._lock:
            _ref = self._hash_ref_dict.get(_identifier)
            if _ref:
                _tracker = self._ref_tracker_dict[_ref]
                return _tracker, False
            else:
                _ref = _WeakRef()
                _tracker = _HardlinkTracker(path, _ref, nlink - 1)

                self._hash_ref_dict[_identifier] = _ref
                self._ref_tracker_dict[_ref] = _tracker
                return _tracker, True


@dataclass
class RegularStats:
    """processed_list have dictionaries as follows:
    {"size": int}  # file size
    {"elapsed": int}  # elapsed time in seconds
    {"op": str}  # operation. "copy", "link" or "download"
    {"errors": int}  # number of errors that occurred when downloading.
    """

    op: str = ""
    size: int = 0
    elapsed: int = 0
    errors: int = 0


class CreateRegularStatsCollector:
    COLLECT_INTERVAL = cfg.STATS_COLLECT_INTERVAL

    def __init__(
        self,
        store: OTAUpdateStatsCollector,
        *,
        total_regular_num: int,
        max_concurrency_tasks: int,
    ) -> None:
        self._que = queue.Queue()
        self._store = store

        self.abort_event = Event()
        self.finished_event = Event()
        self.last_error = None
        self.se = Semaphore(max_concurrency_tasks)
        self.total_regular_num = total_regular_num

    def acquire_se(self):
        """Acquire se for dispatching task to threadpool,
        block if concurrency limit is reached."""
        self.se.acquire()

    def release_se(self):
        self.se.release()

    def collector(self):
        _staging: List[RegularStats] = []
        _cur_time = time.time()
        while True:
            if self.abort_event.is_set():
                logger.error("abort event is set, collector exits")
                break

            if self._store.get_processed_num() < self.total_regular_num:
                try:
                    sts = self._que.get_nowait()
                    _staging.append(sts)
                except queue.Empty:
                    # if no new stats available, wait <_interval> time
                    time.sleep(self.COLLECT_INTERVAL)
            else:
                # all sts are processed
                self.finished_event.set()
                break

            # collect stats every <_interval> seconds
            if _staging and time.time() - _cur_time > self.COLLECT_INTERVAL:
                with self._store.staging_changes() as staging_storage:
                    staging_storage.regular_files_processed += len(_staging)

                    for st in _staging:
                        _suffix = st.op
                        if _suffix in {"copy", "link", "download"}:
                            staging_storage[f"files_processed_{_suffix}"] += 1
                            staging_storage[f"file_size_processed_{_suffix}"] += st.size
                            staging_storage[f"elapsed_time_{_suffix}"] += int(
                                st.elapsed * 1000
                            )

                            if _suffix == "download":
                                staging_storage[f"errors_{_suffix}"] += st.errors

                    # cleanup already collected stats
                    _staging.clear()

                _cur_time = time.time()

    def callback(self, fut: Future):
        """Callback for create regular files
        sts will also being put into que from this callback
        """
        try:
            sts: List[RegularStats] = fut.result()
            for st in sts:
                self._que.put_nowait(st)

            self.release_se()
        except Exception as e:
            self.last_error = e
            # if any task raises exception,
            # interrupt the background collector
            self.abort_event.set()

    def wait(self):
        """Waits until all regular files have been processed
        and detect any exceptions raised by background workers.

        Raise:
            Last error happens among worker threads.
        """
        # loop detect exception
        while not self.finished_event.is_set():
            # if done_event is set before all tasks finished,
            # it means exception happened
            time.sleep(self.COLLECT_INTERVAL)
            if self.abort_event.is_set():
                logger.error(
                    f"create_regular_files failed, last error: {self.last_error!r}"
                )
                raise self.last_error


class RegularInfSet(OrderedDict[RegularInf, None]):
    """Use RegularInf as key, and RegularInf use path: Path as hash key."""

    def add(self, entry: RegularInf):
        self[entry] = None

    def remove(self, entry: RegularInf):
        del self[entry]

    def iter_entries(self) -> Iterator[Tuple[bool, RegularInf]]:
        while True:
            try:
                entry, _ = self.popitem()
                yield len(self) == 0, entry
            except KeyError:
                break


class RegularDelta(Dict[str, RegularInfSet]):
    def __len__(self) -> int:
        return sum([len(_set) for _, _set in self.items()])

    def add_entry(self, entry: RegularInf):
        _hash = entry.sha256hash
        if _hash in self:
            self[_hash].add(entry)
        else:
            _new_set = RegularInfSet()
            _new_set.add(entry)
            self[_hash] = _new_set

    def remove_entry(self, entry: RegularInf):
        _hash = entry.sha256hash
        if _hash not in self:
            return

        _set = self[_hash]
        _set.remove(entry)
        if len(_set) == 0:  # cleanup empty hash group
            del self[_hash]

    def merge_entryset(self, _hash: str, _other: RegularInfSet):
        if _hash not in self:
            return

        self[_hash].update(_other)

    def contains_hash(self, _hash: str) -> bool:
        return _hash in self

    def contains_entry(self, entry: RegularInf):
        if entry.sha256hash in self:
            _set: RegularInfSet = self[entry.sha256hash]
            return entry in _set

        return False

    def contains_path(self, _hash: str, path: Path):
        if _hash in self:
            _set: RegularInfSet = self[_hash]
            return path in _set

        return False


def create_regular_inf(fpath: Union[str, Path], *, _hash: str = None) -> RegularInf:
    # create a new instance of path
    path = Path(fpath)
    stat = path.stat()

    nlink, inode = None, None
    if stat.st_nlink > 0:
        nlink = stat.st_nlink
        inode = stat.st_ino

    _base_root = "/boot" if str(path).startswith("/boot") else "/"

    if _hash is None:
        _hash = file_sha256(fpath)

    res = RegularInf(
        mode=stat.st_mode,
        uid=stat.st_uid,
        gid=stat.st_gid,
        size=stat.st_size,
        nlink=nlink,
        sha256hash=_hash,
        path=path,
        inode=inode,
        _base_root=_base_root,
    )

    return res


@dataclass
class RegInfTuple:
    path: str
    reginf: RegularInf = None
    _type: Literal["_rm", "_hold"] = "_rm"


@dataclass
class DeltaBundle:
    _rm: List[str]
    _hold: RegularDelta
    _new: RegularDelta
    ref_root: Path
    recycle_folder: Path
    total_regular_nums: int


class DeltaGenerator:
    # folders to scan on bank
    TARGET_FOLDERS: ClassVar[List[str]] = [
        "/etc",
        "/greengrass",
        "/home/autoware",
        "/var/lib",
        "/opt",
        "/usr",
        "/var/lib",
    ]
    MAX_FOLDER_DEEPTH = 16
    MAX_FILENUM_PER_FOLDER = 1024

    def __init__(
        self,
        old_reg: Path,
        new_reg: Path,
        *,
        ref_root: Path,
        recycle_folder: Path,
    ) -> None:
        """
        Attrs:
            bank_root: the root of the bank(old) that we calculate delta from.
        """
        self._old_reg = old_reg
        self._new_reg = new_reg

        self._ref_root = ref_root
        self._recycle_folder = recycle_folder

        # generate delta
        self._cal_and_prepare_old_slot_delta()

    @staticmethod
    def _parse_reginf(reginf_file: Union[Path, str]) -> Tuple[RegularDelta, int]:
        _new_delta = RegularDelta()
        with open(reginf_file, "r") as f:
            with ProcessPoolExecutor() as pool:
                for count, entry in enumerate(pool.map(RegularInf, f, chunksize=2048)):
                    _new_delta.add_entry(entry)

        return _new_delta, count + 1

    def _process_file_in_old_slot(self, fpath: Path) -> RegInfTuple:
        if not fpath.is_file():
            return RegInfTuple(str(fpath))

        # NOTE: should always use canonical_fpath
        _canonical_fpath = Path("/") / fpath.relative_to(self._ref_root)
        _canonical_fpath_str = str(_canonical_fpath)

        _recycled_entry = self._recycle_folder / _hash
        if _recycled_entry.is_file():
            return RegInfTuple(_canonical_fpath_str)

        _hash = file_sha256(fpath)
        # collect this entry as the hash existed in _new
        if self._new_delta.contains_hash(_hash):
            shutil.copy(_recycled_entry, fpath)

        # check whether this path should be remained
        if self._new_delta.contains_path(_hash, _canonical_fpath_str):
            entry = create_regular_inf(fpath, _hash=_hash)
            # NOTE: re-asign the path
            entry.path = _canonical_fpath
            return RegInfTuple(_canonical_fpath, reginf=entry, _type="_hold")
        else:
            return RegInfTuple(_canonical_fpath)

    def _cal_and_prepare_old_slot_delta(self):
        """
        NOTE: all local copies are ready after this method
        """
        # init
        self._new_delta, self.total_new_num = self._parse_reginf(self._new_reg)
        self._hold_delta = RegularDelta()
        self._rm_list: List[str] = []

        # phase 1: scan old slot and generate delta based on path,
        #          group files into many hash group,
        #          each hash group is a set contains RegularInf(s) with path as key.
        with ThreadPoolExecutor() as pool:
            for _root in self.TARGET_FOLDERS:
                root_folder = self._ref_root / Path(_root).relative_to("/")

                for dirpath, dirnames, filenames in os.walk(
                    root_folder, topdown=True, followlinks=False
                ):
                    cur_dir = Path(dirpath)
                    if len(cur_dir.parents) > self.MAX_FOLDER_DEEPTH:
                        logger.warning(f"reach max_folder_deepth on {cur_dir!r}, skip")
                        dirnames.clear()
                        continue

                    # skip files that over the max_filenum_per_folder
                    futs: List[Future] = []
                    for fname in filenames[: self.MAX_FILENUM_PER_FOLDER]:
                        futs.append(
                            pool.submit(self._process_file_in_old_slot, cur_dir / fname)
                        )

                    done, _ = wait(futs)
                    for fut in done:
                        try:
                            res: RegInfTuple = fut.result()
                        except Exception as e:
                            raise CreateStandbySlotInternalError from e

                        if res._type == "_rm":
                            self._rm_list.append(res.path)
                        elif res._type == "_hold":
                            self._hold_delta.add_entry(res.reginf)
                            self._new_delta.remove_entry(res.reginf)

        # phase 2: parse _new_delta and compare to _hold by hash,
        #          merging same hash group if possible.
        _optimized: List[str] = []
        for _hash, _entryset in self._new_delta.items():
            if self._hold_delta.contains_hash(_hash):
                # specially label this hash group
                self._hold_delta.merge_entryset(_entryset)

        for _hash in _optimized:
            del self._new_delta[_hash]

    ###### public API ######
    def get_delta(self) -> DeltaBundle:
        return DeltaBundle(
            _rm=self._rm_list,
            _hold=self._hold_delta,
            _new=self._new_delta,
            ref_root=self._ref_root,
            recycle_folder=self._recycle_folder,
            total_regular_nums=self.total_new_num,
        )
