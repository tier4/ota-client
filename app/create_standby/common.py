r"""Common used helpers, classes and functions for different bank creating methods."""
from abc import abstractmethod
from http import cookies
import os
import queue
import shutil
import time
import weakref
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Semaphore
from typing import (
    Any,
    Callable,
    ClassVar,
    Generator,
    List,
    Dict,
    Protocol,
    Set,
    Tuple,
    Union,
)

from app.common import file_sha256
from app.configs import config as cfg
from app.ota_metadata import OtaMetadata, RegularInf
from app import log_util
from app.update_stats import OTAUpdateStatsCollector

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


@dataclass
class UpdateMeta:
    """Meta info for standby slot creator to update slot."""

    cookies: Dict[str, Any]  # cookies needed for requesting remote ota files
    metadata: OtaMetadata  # meta data for the update request
    url_base: str  # base url of the remote ota image
    standby_slot: str  # path(mount point) to the standby slot
    reference_slot: str  # path to the reference slot that we used to copy files from
    boot_dir: str


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
    stats_tracker: OTAUpdateStatsCollector
    status_updator: Callable

    @abstractmethod
    def create_standby_bank(self):
        ...


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
                with self._store.acquire_staging_storage() as staging_storage:
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

            # logger.info(
            #     f"{self._store.get_processed_num()=}, {self.total_regular_num=}, {self._store.get_snapshot()!r}"
            # )

    def callback(self, fut: Future):
        """Callback for create regular files
        sts will also being put into que from this callback
        """
        try:
            sts: List[RegularStats] = fut.result()
            for st in sts:
                self._que.put_nowait(st)

            self.se.release()
        except Exception as e:
            self.last_error = e
            # if any task raises exception,
            # interrupt the background collector
            self.abort_event.set()
            raise

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
                raise self.last_error from None


class RegularInfSet(Set[RegularInf]):
    def __init__(self, __iterable) -> None:
        super().__init__(__iterable)
        self.entry_to_be_collected: RegularInf = None
        self.skip_cleanup = False

    def iter_entries(self) -> Generator[Tuple[bool, RegularInf], None, None]:
        while True:
            try:
                entry = self.pop()
                yield len(self) == 0, entry
            except KeyError:
                break

    def ensure_copy(self, skip_verify: bool, *, src_root: Path) -> bool:
        """Collect one entry within this hash group."""
        if self.entry_to_be_collected is not None:
            return True

        for entry in self:
            # check and verify entry, if valid, labeled as to_be_collected.
            if skip_verify or (
                entry.exists(src_root=src_root) and entry.verify_file(src_root=src_root)
            ):
                self.entry_to_be_collected = entry
                return True

        return False

    def collect_entries_to_be_recycled(self, dst: Path, *, root: Path):
        if self.entry_to_be_collected is not None:
            try:
                entry = self.entry_to_be_collected
                entry.copy2dst(dst / entry.sha256hash, root=root)
            except (FileNotFoundError, shutil.SameFileError):
                pass  # ignore failure

    def is_empty(self):
        """NOTE: do not cleanup if entry_to_be_collected is assigned"""
        return self.entry_to_be_collected is None and len(self) == 0


class RegularDelta(Dict[str, RegularInfSet]):
    def __len__(self) -> int:
        return sum([len(_set) for _, _set in self.items()])

    def skip_cleanup(self, _hash: str):
        if _hash in self:
            self[_hash].skip_cleanup = True

    def add_entry(self, entry: RegularInf):
        _hash = entry.sha256hash
        if _hash in self:
            self[_hash].add(entry)
        else:
            self[_hash] = RegularInfSet([entry])

    def remove_entry(self, entry: RegularInf):
        _hash = entry.sha256hash
        if _hash not in self:
            return

        _set = self[_hash]
        _set.remove(entry)
        if _set.is_empty():  # cleanup empty hash group
            del self[_hash]

    def merge_entryset(self, _hash: str, _pathset: RegularInfSet):
        if _hash not in self:
            return

        self[_hash].update(_pathset)

    def ensure_copy(self, _hash: str, *, root: Path, skip_verify=False):
        """
        Args:
            skip_verify: if the entry has been verified,
                no need to double verify it.
        """
        if _hash not in self:
            return

        self[_hash].ensure_copy(skip_verify, src_root=root)

    def contains_entry(self, entry: RegularInf):
        if entry.sha256hash in self:
            _set: RegularInfSet = self[entry.sha256hash]
            return entry in _set

        return False

    def contains_hash(self, _hash: str) -> bool:
        return _hash in self


def create_regular_inf(fpath: Union[str, Path], *, root: Path) -> RegularInf:
    # create a new instance of path
    path = Path(fpath)
    stat = path.stat()

    nlink, inode = None, None
    if stat.st_nlink > 0:
        nlink = stat.st_nlink
        inode = stat.st_ino

    _base_root = "/boot" if str(path).startswith("/boot") else "/"

    res = RegularInf(
        mode=stat.st_mode,
        uid=stat.st_uid,
        gid=stat.st_gid,
        size=stat.st_size,
        nlink=nlink,
        sha256hash=file_sha256(fpath),
        path=path,
        inode=inode,
        _base_root=_base_root,
    )

    return res


class _RegularDeltaCollector:
    def __init__(self, delta: RegularDelta) -> None:
        self._lock = Lock()
        self._store = delta

    def collect_callback(self, fut: Future):
        try:
            entry: RegularInf = fut.result()
            with self._lock:
                self._store.add_entry(entry)
        except Exception:
            pass


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

    def __init__(self, old_reg, new_reg, *, ref_root: Path) -> None:
        """
        Attrs:
            bank_root: the root of the bank(old) that we calculate delta from.
        """
        self._old_reg = old_reg
        self._new_reg = new_reg
        self._ref_root = ref_root

    def _calculate_current_bank_regulars(self) -> Tuple[bool, RegularDelta]:
        skip_verify, _rm = False, RegularDelta()

        if Path(self._old_reg).is_file():
            with open(self._old_reg, "r") as f:
                for l in f:
                    entry = RegularInf(l)
                    _rm.add_entry(entry)
        else:
            _collector = _RegularDeltaCollector(_rm)
            with ThreadPoolExecutor(thread_name_prefix="calculate_delta") as pool:
                for _root in self.TARGET_FOLDERS:
                    root_folder = self._ref_root / Path(_root).relative_to("/")

                    for dirpath, dirnames, filenames in os.walk(
                        root_folder, topdown=True, followlinks=False
                    ):
                        cur_dir = Path(dirpath)
                        if len(cur_dir.parents) > self.MAX_FOLDER_DEEPTH:
                            logger.warning(
                                f"reach max_folder_deepth on {cur_dir!r}, skip"
                            )
                            dirnames.clear()
                            continue

                        # skip files that over the max_filenum_per_folder
                        for fname in filenames[: self.MAX_FILENUM_PER_FOLDER]:
                            pool.submit(
                                create_regular_inf, cur_dir / fname, root=self._ref_root
                            ).add_done_callback(_collector.collect_callback)

            skip_verify = True

        logger.info(f"{len(_rm)=}")

        return skip_verify, _rm

    def calculate_delta(self) -> Tuple[RegularDelta, RegularDelta, RegularDelta, int]:
        skip_verify, _rm = self._calculate_current_bank_regulars()
        _hold, _new = RegularDelta(), RegularDelta()

        # 1st-pass, comparing old and new by path
        with open(self._new_reg, "r") as f:
            for count, l in enumerate(f):
                entry = RegularInf.parse_reginf(l)

                if _rm.contains_entry(entry):
                    # this path exists in both old and new
                    _hold.add_entry(entry)
                    _hold.ensure_copy(
                        entry.sha256hash,
                        root=self._ref_root,
                        skip_verify=skip_verify,
                    )

                    _rm.remove_entry(entry)
                else:
                    # completely new path
                    _new.add_entry(entry)

            count += 1

        # 2nd-pass: comparing _rm with _new by hash
        #   cover the case that paths in _rm and _new have same hash
        for _hash in _rm:
            if _new.contains_hash(_hash):
                # for hash that exists in both _rm and _new,
                # recycle one entry in _rm to recycle folder.
                _rm.ensure_copy(_hash, root=self._ref_root)

        # 3rd-pass: comparing _new and _hold by hash
        #   cover the case that new paths point to files that
        #   existed in the _hold(detected by hash).
        for _hash in _new:
            if _hold.contains_hash(_hash):
                # specially label this hash group
                _hold.skip_cleanup(_hash)

        return _new, _hold, _rm, count
