r"""Common used helpers, classes and functions for different bank creating methods."""
import os
import shutil
import time
import weakref
from abc import abstractmethod
from concurrent.futures import (
    Future,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    wait,
)
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock
from typing import (
    Any,
    Callable,
    ClassVar,
    Iterator,
    List,
    Dict,
    Optional,
    OrderedDict,
    Protocol,
    Set,
    Tuple,
    TypeVar,
    Union,
)

from app.common import file_sha256
from app.configs import config as cfg
from app.ota_metadata import OtaMetadata, RegularInf
from app import log_util
from app.update_phase import OTAUpdatePhase
from app.update_stats import OTAUpdateStatsCollector, RegInfProcessedStats

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
    boot_dir: str  # where to populate files under /boot


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
    stats_collector: OTAUpdateStatsCollector
    update_phase_tracker: Callable[[OTAUpdatePhase], None]

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
    def __init__(self):
        super().__init__()
        # for fast lookup regularinf entry
        self._regularinf_set: Set[RegularInf] = set()

    def __len__(self) -> int:
        return sum([len(_set) for _, _set in self.items()])

    def add_entry(self, entry: RegularInf):
        self._regularinf_set.add(entry)

        _hash = entry.sha256hash
        if _hash in self:
            self[_hash].add(entry)
        else:
            _new_set = RegularInfSet()
            _new_set.add(entry)
            self[_hash] = _new_set

    def remove_entry(self, entry: RegularInf):
        self._regularinf_set.discard(entry)

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
        for entry, _ in _other.items():
            self._regularinf_set.add(entry)

    def contains_hash(self, _hash: str) -> bool:
        return _hash in self

    def contains_entry(self, entry: RegularInf):
        return entry in self._regularinf_set

    def contains_path(self, path: Path):
        return path in self._regularinf_set


@dataclass
class DeltaBundle:
    _rm: List[str]
    _new: RegularDelta
    ref_root: Path
    recycle_folder: Path
    total_regular_num: int


# whether to enable full scan on all files,
# or fall back to only scan paths that also exists in new img
_SCAN_MODE = TypeVar("_SCAN_MODE")
_FULL_SCAN = "FULL_SCAN"
_MATCH_ONLY = "MATCH_ONLY"


class DeltaGenerator:
    # folders to scan on bank
    # Dict[<folder_name>, <full_scan>]
    TARGET_FOLDERS: ClassVar[Dict[str, _SCAN_MODE]] = {
        "/etc": _MATCH_ONLY,
        "/opt": _MATCH_ONLY,
        "/usr": _FULL_SCAN,
        "/var/lib": _FULL_SCAN,
        "/home/autoware": _MATCH_ONLY,
    }
    MAX_FOLDER_DEEPTH = 16
    MAX_FILENUM_PER_FOLDER = 1024

    def __init__(
        self,
        old_reg: Path,
        new_reg: Path,
        *,
        ref_root: Path,
        recycle_folder: Path,
        stats_collector: OTAUpdateStatsCollector,
    ) -> None:
        """
        Attrs:
            bank_root: the root of the bank(old) that we calculate delta from.
        """
        self._old_reg = old_reg
        self._new_reg = new_reg

        self._stats_collector = stats_collector
        self._ref_root = ref_root
        self._recycle_folder = recycle_folder

        # generate delta
        self._cal_and_prepare_old_slot_delta()

    @staticmethod
    def _parse_reginf(reginf_file: Union[Path, str]) -> Tuple[RegularDelta, int]:
        _new_delta = RegularDelta()
        with open(reginf_file, "r") as f:
            with ProcessPoolExecutor() as pool:
                for count, entry in enumerate(
                    pool.map(RegularInf.parse_reginf, f, chunksize=2048)
                ):
                    _new_delta.add_entry(entry)

        return _new_delta, count + 1

    def _process_file_in_old_slot(
        self, fpath: Path, *, _hash: str = None
    ) -> Optional[str]:
        # NOTE: should always use canonical_fpath in RegularInf and in rm_list
        _canonical_fpath = Path("/") / fpath.relative_to(self._ref_root)
        _canonical_fpath_str = str(_canonical_fpath)

        # ignore non-file file
        if not fpath.is_file():
            return

        # TODO: if old_reg is available, we can get the hash of the path
        # without hashing it in the first place
        if _hash is None:
            _hash = file_sha256(fpath)

        _recycled_entry = self._recycle_folder / _hash
        # collect this entry as the hash existed in _new
        # and not yet being collected
        if not _recycled_entry.is_file() and self._new_delta.contains_hash(_hash):
            _start = time.thread_time()
            shutil.copy(fpath, _recycled_entry)

            # report to the ota update stats collector
            self._stats_collector.report(
                RegInfProcessedStats(
                    op="copy",
                    size=fpath.stat().st_size,
                    elapsed=time.thread_time() - _start,
                )
            )

        # check whether this path should be remained
        if not self._new_delta.contains_path(_canonical_fpath):
            return _canonical_fpath_str

    def _cal_and_prepare_old_slot_delta(self):
        """
        NOTE: all local copies are ready after this method
        """
        self._new_delta, self.total_new_num = self._parse_reginf(self._new_reg)
        self._rm_list: Set[str] = set()

        # scan old slot and generate delta based on path,
        # group files into many hash group,
        # each hash group is a set contains RegularInf(s) with path as key.
        #
        # if the scanned file's hash existed in _new,
        # collect this file to the recycle folder if not yet being collected.

        # TODO: if old_reg is available, only scan files that presented in the old_reg
        with ThreadPoolExecutor() as pool:
            for _root, scan_mode in self.TARGET_FOLDERS.items():
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
                        fpath = cur_dir / fname

                        # in MATCH_ONLY scan mode, skip paths that don't exist
                        # in the new img to prevent scanning unintentionally files.
                        if scan_mode == _MATCH_ONLY:
                            canonical_path = Path("/") / fpath.relative_to(
                                self._ref_root
                            )
                            if not self._new_delta.contains_path(canonical_path):
                                continue

                        futs.append(pool.submit(self._process_file_in_old_slot, fpath))

                    done, _ = wait(futs)
                    for fut in done:
                        try:
                            if res := fut.result():
                                self._rm_list.add(res)
                        except Exception as e:
                            logger.warning(
                                f"scan one file failed under {cur_dir=}: {e!r}, ignored"
                            )

    ###### public API ######
    def get_delta(self) -> DeltaBundle:
        return DeltaBundle(
            _rm=self._rm_list,
            _new=self._new_delta,
            ref_root=self._ref_root,
            recycle_folder=self._recycle_folder,
            total_regular_num=self.total_new_num,
        )
