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


from __future__ import annotations
import functools
import os
import random
import time
import shutil
from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    wait,
)
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from threading import Event, Lock
from typing import (
    Any,
    Iterator,
    List,
    Dict,
    Optional,
    OrderedDict,
    Set,
    Tuple,
    Union,
)
from weakref import WeakKeyDictionary, WeakValueDictionary

from otaclient._utils.typing import StrOrPath
from otaclient._utils.unix import (
    ParsedPasswd,
    ParsedGroup,
    map_gid_by_grpnam,
    map_uid_by_pwnam,
)
from ..common import create_tmp_fname
from ..configs import config as cfg
from ..ota_metadata import OTAMetadata, MetafilesV1
from ..proto.wrapper import RegularInf, DirectoryInf
from .. import log_setting
from ..update_stats import (
    OTAUpdateStatsCollector,
    RegProcessOperation,
    RegInfProcessedStats,
)

logger = log_setting.get_logger(__name__)


class _WeakRef:
    pass


class _HardlinkTracker:
    POLLINTERVAL = 0.1

    def __init__(self, first_copy_path: str, ref: _WeakRef, count: int):
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

    def subscribe(self) -> str:
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

    def subscribe_no_wait(self) -> str:
        return self.first_copy_path


class HardlinkRegister:
    def __init__(self):
        self._lock = Lock()
        self._hash_ref_dict: WeakValueDictionary[str, _WeakRef] = WeakValueDictionary()
        self._ref_tracker_dict: WeakKeyDictionary[
            _WeakRef, _HardlinkTracker
        ] = WeakKeyDictionary()

    def get_tracker(
        self, _identifier: Any, path: str, nlink: int
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


class RegularDelta(Dict[bytes, Set[RegularInf]]):
    """Dict[bytes, Set[RegularInf]]"""

    def __init__(self):
        self._path_set: Set[str] = set()

    def __len__(self) -> int:
        return sum([len(_set) for _, _set in self.items()])

    def add_entry(self, entry: RegularInf):
        self._path_set.add(entry.path)
        if (_hash := entry.sha256hash) in self:
            self[_hash].add(entry)
        else:
            (_new_set := set()).add(entry)
            self[_hash] = _new_set

    def contains_path(self, path: Union[Path, str]):
        return str(path) in self._path_set


@dataclass
class DeltaBundle:
    """NOTE: all paths are canonical!"""

    # ------ generated delta ------ #
    # rm_delta: a list of files that presented at
    #           delta_src rootfs but don't presented
    #           at new OTA image
    rm_delta: List[str]
    # download_list: a list of files that presented in
    #                the new OTA image but are not available
    #                locally
    download_list: List[RegularInf]
    # new_delta: the generated delta, used by create_standby
    #            implementation to update the standby slot
    new_delta: RegularDelta
    # new_dirs: all the dirs presented in the new OTA image
    new_dirs: OrderedDict[DirectoryInf, None]

    # ------ misc ------ #
    # delta_src: the src when calculating delta against
    #            target new OTA image
    delta_src: Path
    # delta_files_dir: the folder that holds the files
    #                  that will be applied to the standby slot
    delta_files_dir: Path
    total_regular_num: int
    total_download_files_size: int  # in bytes

    # getter API
    # NOTE: all the getter APIs will transfer the ref
    #       to the caller, and then release it.

    def get_rm_delta(self) -> Iterator[str]:
        if not (_rm_delta := self.rm_delta):
            return []  # type: ignore
        self.rm_delta = None  # type: ignore

        def _gen():
            while _rm_delta:
                yield _rm_delta.pop()

        return _gen()

    def get_new_delta(self) -> RegularDelta:
        if not (_new_delta := self.new_delta):
            return {}  # type: ignore
        self.new_delta = None  # type: ignore
        return _new_delta

    def get_download_list(self) -> Iterator[RegularInf]:
        if not (_download_list := self.download_list):
            return []  # type: ignore
        self.download_list = None  # type: ignore

        def _gen():
            while _download_list:
                yield _download_list.pop()

        return _gen()

    def get_new_dirs(self) -> Iterator[DirectoryInf]:
        if not (_dirs := self.new_dirs):
            return []  # type: ignore
        self.new_dirs = None  # type: ignore

        def _gen():
            while _dirs:
                yield _dirs.popitem()[0]

        return _gen()


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
        *,
        ota_metadata: OTAMetadata,
        delta_src: Path,
        local_copy_dir: Path,
        stats_collector: OTAUpdateStatsCollector,
    ) -> None:
        self._ota_metadata = ota_metadata

        # delta
        self._new = RegularDelta()
        self._rm: List[str] = []
        self._new_dirs: OrderedDict[DirectoryInf, None] = OrderedDict()
        self._new_hash_size_dict: Dict[bytes, int] = {}
        self._download_list: List[RegularInf] = []

        self._stats_collector = stats_collector
        self._delta_src_mount_point = delta_src
        self._local_copy_dir = local_copy_dir

        self.total_regulars_num = 0
        self.total_download_files_size = 0

    def _prepare_local_copy_from_active_slot(
        self,
        fpath: Path,
        *,
        expected_hash: Optional[str] = None,
    ) -> None:
        """Hash(and verify the file) and prepare a copy for it in standby slot.

        Params:
            fpath: the path to the file to be verified
            expected_hash: (optional) if we have the information for this file
                (by stored OTA image meta for active slot), use it make the
                verification process faster.

        NOTE: verify the file before copying to the standby slot!
        """
        # if we have information related to this file in advance(with saved
        #   OTA image meta for the active slot), use this information to
        #   pre-check the file.
        if expected_hash:
            if expected_hash not in self._new_hash_size_dict:
                return  # skip uneeded/already prepared local copy
            # check file size with size information
            try:
                expected_size = self._new_hash_size_dict[expected_hash]
                if expected_size and expected_size != fpath.stat().st_size:
                    return
            except KeyError:
                pass

        start_time = time.thread_time_ns()
        tmp_f = self._local_copy_dir / create_tmp_fname()
        try:
            hash_f = sha256()
            with open(fpath, "rb") as src, open(tmp_f, "wb") as tmp_dst:
                while chunk := src.read(cfg.LOCAL_CHUNK_SIZE):
                    hash_f.update(chunk)
                    tmp_dst.write(chunk)
            hash_value = hash_f.hexdigest()
            if expected_hash and expected_hash != hash_value:
                return  # skip invalid file

            # this tmp is valid, use it as local copy
            try:  # remove from new_hash_list to mark this hash as prepared
                self._new_hash_size_dict.pop(hash_f.digest())
            except KeyError:
                return  # this hash has already been prepared by other thread
            tmp_f.rename(self._local_copy_dir / hash_value)
        finally:
            tmp_f.unlink(missing_ok=True)

        # report to the ota update stats collector
        self._stats_collector.report_prepare_local_copy(
            RegInfProcessedStats(
                op=RegProcessOperation.PREPARE_LOCAL_COPY,
                size=fpath.stat().st_size,
                elapsed_ns=time.thread_time_ns() - start_time,
            ),
        )

    def _process_delta_src(self):
        logger.debug("process delta src, generate delta and prepare local copy...")
        _canonical_root = Path("/")

        # scan old slot and generate delta based on path,
        # group files into many hash group,
        # each hash group is a set contains RegularInf(s) with path as key.
        #
        # if the scanned file's hash existed in _new,
        # collect this file to the recycle folder if not yet being collected.
        with ThreadPoolExecutor(thread_name_prefix="scan_slot") as pool:
            for curdir, dirnames, filenames in os.walk(
                self._delta_src_mount_point, topdown=True, followlinks=False
            ):
                delta_src_curdir_path = Path(curdir)
                canonical_curdir_path = (
                    _canonical_root
                    / delta_src_curdir_path.relative_to(self._delta_src_mount_point)
                )
                logger.debug(f"{delta_src_curdir_path=}, {canonical_curdir_path=}")

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
                # NOTE: the root folder must be fully scanned
                # NOTE: DirectoryInf can only be compared with str, not Path
                dir_should_skip = True
                if (
                    canonical_curdir_path == _canonical_root
                    or str(canonical_curdir_path) in self._new_dirs
                ):
                    dir_should_skip = False
                # check if we neede to fully scan this folder
                dir_should_fully_scan = False
                for parent in reversed(canonical_curdir_path.parents):
                    if str(parent) in self.FULL_SCAN_PATHS:
                        dir_should_fully_scan = True
                        break
                logger.debug(
                    f"{dir_should_skip=}, {dir_should_fully_scan=}: {delta_src_curdir_path=}"
                )
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

                # process the files under this dir
                futs: List[Future] = []
                for fname in filenames[: self.MAX_FILENUM_PER_FOLDER]:
                    delta_src_fpath = delta_src_curdir_path / fname
                    logger.debug(f"[process_delta_src] process {delta_src_fpath}")
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

                    futs.append(
                        pool.submit(
                            self._prepare_local_copy_from_active_slot, delta_src_fpath
                        )
                    )
                # wait for all files under this dir to be finished
                logger.debug(f"{delta_src_curdir_path=} done")
                wait(futs)

        # calculate the files list that we should download from remote
        # the hash in the self._hash_set after local delta preparation representing
        # the file we need to download from remote
        for _hash, _reginf_set in self._new.items():
            if _hash in self._new_hash_size_dict:
                # pick one entry from the reginf set for downloading
                _entry = next(iter(_reginf_set))
                self._download_list.append(_entry)
                self.total_download_files_size += _entry.size if _entry.size else 0
        self._new_hash_size_dict.clear()

    # API

    def calculate_and_process_delta(self) -> DeltaBundle:
        # pre-load dirs info
        for _dir in self._ota_metadata.iter_metafile(MetafilesV1.DIRECTORY_FNAME):
            self._new_dirs[_dir] = None
        # pre-load from new regulars.txt
        _entry: RegularInf
        for _entry in self._ota_metadata.iter_metafile(MetafilesV1.REGULAR_FNAME):
            self.total_regulars_num += 1
            self._new.add_entry(_entry)
            self._new_hash_size_dict[_entry.sha256hash] = _entry.size

        # generate delta and prepare files
        self._process_delta_src()
        logger.info(
            "delta calculation finished: \n"
            f"total_regulars_num: {self.total_regulars_num} \n"
            f"total_download_files_size: {self.total_download_files_size} \n"
            f"rm_list len: {len(self._rm)} \n"
            f"donwload_list len: {len(self._download_list)}"
        )

        # 20230609: shuffle the download list to avoid multiple ECU downloading
        #           the same file at the same time to improve performance and
        #           cache efficiency.
        random.Random(os.urandom(32)).shuffle(self._download_list)

        return DeltaBundle(
            rm_delta=self._rm,
            new_delta=self._new,
            new_dirs=self._new_dirs,
            download_list=self._download_list,
            delta_src=self._delta_src_mount_point,
            delta_files_dir=self._local_copy_dir,
            total_regular_num=self.total_regulars_num,
            total_download_files_size=self.total_download_files_size,
        )


class PersistFilesHandler:
    """Preserving files in persist list from <src_root> to <dst_root>.

    Files being copied will have mode bits preserved,
    and uid/gid preserved with mapping as follow:

        src_uid -> src_name -> dst_name -> dst_uid
        src_gid -> src_name -> dst_name -> dst_gid
    """

    def __init__(
        self,
        src_passwd_file: StrOrPath,
        src_group_file: StrOrPath,
        dst_passwd_file: StrOrPath,
        dst_group_file: StrOrPath,
        *,
        src_root: StrOrPath,
        dst_root: StrOrPath,
    ):
        self._uid_mapper = functools.lru_cache()(
            functools.partial(
                map_uid_by_pwnam,
                src_db=ParsedPasswd(src_passwd_file),
                dst_db=ParsedPasswd(dst_passwd_file),
            )
        )
        self._gid_mapper = functools.lru_cache()(
            functools.partial(
                map_gid_by_grpnam,
                src_db=ParsedGroup(src_group_file),
                dst_db=ParsedGroup(dst_group_file),
            )
        )
        self._src_root = Path(src_root)
        self._dst_root = Path(dst_root)

    def _chown_with_mapping(
        self, _src_stat: os.stat_result, _dst_path: StrOrPath
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

    def _prepare_parent(self, _src_path: Path, _dst_path: Path) -> None:
        if _dst_path.is_dir():  # keep the origin parent on dst as it
            return
        if _dst_path.is_symlink() or _dst_path.is_file():
            _dst_path.unlink(missing_ok=True)
            self._prepare_dir(_src_path, _dst_path)
            return
        if _dst_path.exists():
            raise ValueError(
                f"{_dst_path=} is not a normal file/symlink/dir, cannot cleanup"
            )
        self._prepare_dir(_src_path, _dst_path)

    # API

    def preserve_persist_entry(self, _persist_entry: StrOrPath):
        logger.info(
            f"preserving {_persist_entry} from {self._src_root} to {self._dst_root}"
        )
        # persist_entry in persists.txt must be rooted at /
        origin_entry = Path(_persist_entry).relative_to(cfg.DEFAULT_ACTIVE_ROOTFS)
        src_path = self._src_root / origin_entry
        dst_path = self._dst_root / origin_entry

        # ------ prepare parents ------ #
        for _parent in reversed(origin_entry.parents):
            self._prepare_parent(self._src_root / _parent, self._dst_root / _parent)

        # ------ prepare entry itself ------ #
        # NOTE: always check if symlink first!
        if src_path.is_symlink():
            self._rm_target(dst_path)
            self._prepare_symlink(src_path, dst_path)
            return

        if src_path.is_file():
            self._rm_target(dst_path)
            self._prepare_file(src_path, dst_path)
            return

        # we only process normal file/symlink/dir
        if not src_path.is_dir():
            raise ValueError(
                f"{src_path=} must be presented and either a file/symlink/dir"
            )

        # for src as dir, cleanup dst_dirc,
        # dive into src_dir and preserve everything under the src dir
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
