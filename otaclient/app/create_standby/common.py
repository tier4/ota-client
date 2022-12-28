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


r"""Common used helpers, classes and functions for different bank creating methods."""
import os
import shutil
import time
from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    wait,
)
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock
from typing import (
    Iterator,
    List,
    Dict,
    Optional,
    OrderedDict,
    Set,
    Tuple,
    Union,
    Iterable,
)
from weakref import WeakKeyDictionary, WeakValueDictionary

from ..common import file_sha256
from ..configs import config as cfg
from ..ota_metadata import DirectoryInf, RegularInf
from .. import log_setting
from ..update_stats import (
    OTAUpdateStatsCollector,
    RegInfProcessedStats,
    RegProcessOperation,
)

logger = log_setting.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


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
        self._hash_ref_dict: "WeakValueDictionary[str, _WeakRef]" = (
            WeakValueDictionary()
        )
        self._ref_tracker_dict: "WeakKeyDictionary[_WeakRef, _HardlinkTracker]" = (
            WeakKeyDictionary()
        )

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
        _len, _count = len(self), 0
        for entry, _ in self.items():
            _count += 1
            yield _len == _count, entry


class RegularDelta(Dict[str, RegularInfSet]):
    """Dict[str, RegularInfSet]"""

    def __init__(self):
        super().__init__()
        # for fast lookup regularinf entry
        self._pathset: Set[Path] = set()

    def __len__(self) -> int:
        return sum([len(_set) for _, _set in self.items()])

    def add_entry(self, entry: RegularInf):
        self._pathset.add(entry.path)

        _hash = entry.sha256hash
        if _hash in self:
            self[_hash].add(entry)
        else:
            _new_set = RegularInfSet()
            _new_set.add(entry)
            self[_hash] = _new_set

    def merge_entryset(self, _hash: str, _other: RegularInfSet):
        if _hash not in self:
            return

        self[_hash].update(_other)
        for entry, _ in _other.items():
            self._pathset.add(entry.path)

    def contains_hash(self, _hash: str) -> bool:
        return _hash in self

    def contains_path(self, path: Path):
        return path in self._pathset


@dataclass
class DeltaBundle:
    """NOTE: all paths are canonical!"""

    # delta
    rm_delta: Iterable[str]
    download_list: Iterable[RegularInf]
    new_delta: RegularDelta
    new_dirs: OrderedDict[DirectoryInf, None]

    # misc
    delta_src: Path
    pickup_dir: Path
    total_regular_num: int


class DeltaGenerator:
    # entry under the following folders will be scanned
    # no matter it is existed in new image or not
    FULL_SCAN_PATHS = set(
        [
            "/lib",
            "/var/lib",
            "/usr",
            "/opt/nvidia",
            "/home/autoware/autoware.proj",
        ]
    )

    # introduce limitations here to prevent unexpected
    # scanning in unknown large, deep folders in full
    # scan mode.
    # NOTE: the following settings are enough for most cases
    MAX_FOLDER_DEEPTH = 20
    MAX_FILENUM_PER_FOLDER = 8192

    def __init__(
        self,
        delta_src_reg: Path,  # path to delta_src regulars.txt
        new_reg: Path,  # path to new image regulars.txt
        new_dirs: Path,  # path to dirs.txt
        *,
        delta_src: Path,
        local_copy_dir: Path,
        stats_collector: OTAUpdateStatsCollector,
    ) -> None:
        # delta
        self._new = RegularDelta()
        self._rm: List[str] = []
        self._new_dirs: OrderedDict[DirectoryInf, None] = OrderedDict()
        self._new_hash_list: Set[str] = set()
        self._delta_src_path_hash_dict: Dict[str, str] = {}

        self._stats_collector = stats_collector
        self._delta_src_mp = delta_src
        self._local_copy_dir = local_copy_dir

        self.total_regulars_num = 0

        # pre-load dirs list
        with open(new_dirs, "r") as f:
            for _dir in map(DirectoryInf, f):
                self._new_dirs[_dir] = None
        # pre-load from new regulars.txt
        with open(new_reg, "r") as f:
            for _entry in map(RegularInf.parse_reginf, f):
                self.total_regulars_num += 1
                self._new.add_entry(_entry)
                self._new_hash_list.add(_entry.sha256hash)
        self._stats_collector.store.total_regular_files = self.total_regulars_num
        # pre-load delta src's reginf if presented
        if Path(delta_src_reg).is_file():
            with open(delta_src_reg, "r") as f:
                for _entry in map(RegularInf.parse_reginf, f):
                    self._delta_src_path_hash_dict[str(_entry.path)] = _entry.sha256hash

        # generate delta and prepare files
        self._process_delta_src()

    def _process_file_in_old_slot(
        self, fpath: Path, *, _hash: Optional[str] = None
    ) -> None:
        """Hash(and verify the file) and prepare a copy for it in standby slot."""
        _fhash = file_sha256(fpath)
        if _hash and _hash != _fhash:
            return

        try:
            self._new_hash_list.remove(_fhash)
        except KeyError:
            # this hash has already been prepared
            return

        # collect this entry as the hash existed in _new
        # and not yet being collected
        _start = time.thread_time_ns()
        shutil.copy(fpath, self._local_copy_dir / _fhash)

        # report to the ota update stats collector
        self._stats_collector.report(
            RegInfProcessedStats(
                op=RegProcessOperation.OP_COPY,
                size=fpath.stat().st_size,
                elapsed_ns=time.thread_time_ns() - _start,
            )
        )

    def _process_delta_src(self):
        _canonical_root = Path("/")

        # scan old slot and generate delta based on path,
        # group files into many hash group,
        # each hash group is a set contains RegularInf(s) with path as key.
        #
        # if the scanned file's hash existed in _new,
        # collect this file to the recycle folder if not yet being collected.
        with ThreadPoolExecutor(thread_name_prefix="scan_slot") as pool:
            for curdir, dirnames, filenames in os.walk(
                self._delta_src_mp, topdown=True, followlinks=False
            ):
                delta_src_curdir_path = Path(curdir)
                canonical_curdir_path = (
                    _canonical_root
                    / delta_src_curdir_path.relative_to(self._delta_src_mp)
                )

                # skip folder that exceeds max_folder_deepth,
                # also add these folders to remove list
                if len(delta_src_curdir_path.parents) > self.MAX_FOLDER_DEEPTH:
                    logger.warning(
                        f"reach max_folder_deepth on {delta_src_curdir_path!r}, skip"
                    )
                    self._rm.append(str(canonical_curdir_path))
                    dirnames.clear()
                    continue

                # skip this folder if it doesn't exist on new image,
                # or also not meant to be fully scanned.
                dir_should_skip = False
                if (
                    canonical_curdir_path == _canonical_root
                    or canonical_curdir_path in self._new_dirs
                ):
                    dir_should_skip = True
                # check if we neede to fully scan this folder
                dir_should_fully_scan = False
                for parent in reversed(canonical_curdir_path.parents):
                    if str(parent) in self.FULL_SCAN_PATHS:
                        dir_should_fully_scan = True
                        break
                # should we totally skip folder and all its child folders?
                # if so, discard it and add it to the remove list.
                if dir_should_skip and not dir_should_fully_scan:
                    self._rm.append(str(canonical_curdir_path))
                    dirnames.clear()  # prune the search on all its subfolders
                    continue

                # skip files that over the max_filenum_per_folder,
                # and add these files to remove list
                if len(filenames) > self.MAX_FILENUM_PER_FOLDER:
                    logger.warning(
                        f"reach max_filenum_per_folder on {delta_src_curdir_path}, "
                        "exceeded files will be ignored silently"
                    )
                    self._rm.extend(
                        map(
                            lambda x: str(canonical_curdir_path / x),
                            filenames[self.MAX_FILENUM_PER_FOLDER :],
                        )
                    )

                futs: List[Future] = []
                for fname in filenames[: self.MAX_FILENUM_PER_FOLDER]:
                    delta_src_fpath = delta_src_curdir_path / fname
                    # NOTE: should ALWAYS use canonical_fpath in RegularInf and in rm_list
                    canonical_fpath = canonical_curdir_path / fname

                    # ignore non-file file(include symlink)
                    # NOTE: for in-place update, we will recreate all the symlinks,
                    #       so we first remove all the symlinks
                    if delta_src_fpath.is_symlink() or not delta_src_fpath.is_file():
                        self._rm.append(str(canonical_fpath))
                        continue
                    # in default match_only mode, if the path doesn't exist in new, ignore
                    if not dir_should_fully_scan and not self._new.contains_path(
                        canonical_fpath
                    ):
                        self._rm.append(str(canonical_fpath))
                        continue

                    # if delta_src meta is available, only process the file listed
                    # in the delta_src meta, otherwise move it to rm_list
                    if self._delta_src_path_hash_dict:
                        if _hash := self._delta_src_path_hash_dict.get(
                            str(canonical_fpath)
                        ):
                            futs.append(
                                pool.submit(
                                    self._process_file_in_old_slot,
                                    delta_src_fpath,
                                    _hash=_hash,
                                )
                            )
                        else:
                            self._rm.append(str(canonical_fpath))
                    # delta_src meta is not available,
                    # directly hash and collect the file
                    else:
                        futs.append(
                            pool.submit(self._process_file_in_old_slot, delta_src_fpath)
                        )

                # wait for all tasks to be finished
                wait(futs)

    def _files_to_be_downloaded(self) -> Iterator[RegularInf]:
        # calculate the files list that we should download from remote
        # the hash in the self._hash_set after local delta preparation representing
        # the file we need to download from remote
        for _hash, _reginf_set in self._new.items():
            if _hash in self._new_hash_list:
                # pick one entry from the reginf set for downloading
                _iter = _reginf_set.iter_entries()
                _, _entry = next(_iter)
                yield _entry

    ###### public API ######

    def get_delta(self) -> DeltaBundle:
        return DeltaBundle(
            rm_delta=self._rm,
            new_delta=self._new,
            new_dirs=self._new_dirs,
            download_list=self._files_to_be_downloaded(),
            delta_src=self._delta_src_mp,
            pickup_dir=self._local_copy_dir,
            total_regular_num=self.total_regulars_num,
        )
