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


import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, List
from urllib.parse import quote

from ..errors import NetworkError, OTAError, StandbySlotSpaceNotEnoughError
from ..common import SimpleTasksTracker, OTAFileCacheControl, urljoin_ensure_base
from ..configs import config as cfg
from ..errors import (
    ApplyOTAUpdateFailed,
    OTAErrorUnRecoverable,
    OTAMetaDownloadFailed,
    OTAMetaVerificationFailed,
    UpdateDeltaGenerationFailed,
)
from ..proxy_info import proxy_cfg
from ..downloader import (
    ChunkStreamingError,
    DestinationNotAvailableError,
    DownloadError,
    Downloader,
    ExceedMaxRetryError,
    HashVerificaitonError,
    DownloadFailedSpaceNotEnough,
)
from ..update_stats import (
    OTAUpdateStatsCollector,
    RegInfProcessedStats,
    RegProcessOperation,
)
from ..proto import wrapper
from ..ota_metadata import (
    PersistentInf,
    SymbolicLinkInf,
)
from .. import log_setting

from .common import HardlinkRegister, RegularInfSet, DeltaGenerator
from .interface import StandbySlotCreatorProtocol, UpdateMeta

logger = log_setting.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class RebuildMode(StandbySlotCreatorProtocol):
    MAX_CONCURRENT_TASKS = cfg.MAX_CONCURRENT_TASKS
    OTA_TMP_STORE = cfg.OTA_TMP_STORE
    META_FOLDER = cfg.META_FOLDER

    def __init__(
        self,
        *,
        update_meta: UpdateMeta,
        stats_collector: OTAUpdateStatsCollector,
        update_phase_tracker: Callable[[wrapper.StatusProgressPhase], None],
        downloader: Downloader,
    ) -> None:
        self.cookies = update_meta.cookies
        self.metadata = update_meta.metadata
        self.url_base = update_meta.url_base
        self.stats_collector = stats_collector
        self.update_phase_tracker = update_phase_tracker

        # path configuration
        self.boot_dir = Path(update_meta.boot_dir)
        self.standby_slot_mp = Path(update_meta.standby_slot_mount_point)
        self.reference_slot_mp = Path(update_meta.ref_slot_mount_point)

        # recycle folder, files copied from referenced slot will be stored here,
        # also the meta files will be stored under this folder
        self._recycle_folder = self.standby_slot_mp / Path(
            cfg.OTA_TMP_STORE
        ).relative_to("/")
        self._recycle_folder.mkdir()

        # configure the downloader
        self._downloader = downloader
        self.proxies = None
        if proxy := proxy_cfg.get_proxy_for_local_ota():
            logger.info(f"use {proxy=} for downloading")
            # NOTE: check requests doc for details
            self.proxies = {"http": proxy}

    def _prepare_meta_files(self):
        self.update_phase_tracker(wrapper.StatusProgressPhase.METADATA)
        try:
            for meta_f in self.metadata.get_img_metafiles():
                meta_f_url = urljoin_ensure_base(self.url_base, quote(meta_f.file))
                self._downloader.download(
                    meta_f_url,
                    self._recycle_folder / meta_f.file,
                    digest=meta_f.hash,
                    proxies=self.proxies,
                    cookies=self.cookies,
                    headers={
                        OTAFileCacheControl.header_lower.value: OTAFileCacheControl.no_cache.value
                    },
                )
        except HashVerificaitonError as e:
            raise OTAMetaVerificationFailed from e
        except DestinationNotAvailableError as e:
            raise OTAErrorUnRecoverable from e
        except DownloadError as e:
            raise OTAMetaDownloadFailed from e

    def _cal_and_prepare_delta(self):
        self.update_phase_tracker(wrapper.StatusProgressPhase.REGULAR)

        # TODO 2: old_reg is not used currently
        logger.info("generating delta...")

        regular_inf_fname = self.metadata.regular.file
        dir_inf_fname = self.metadata.directory.file
        try:
            delta_calculator = DeltaGenerator(
                old_reg=Path(self.META_FOLDER) / regular_inf_fname,
                new_reg=self._recycle_folder / regular_inf_fname,
                new_dirs=self._recycle_folder / dir_inf_fname,
                ref_root=self.reference_slot_mp,
                recycle_folder=self._recycle_folder,
                stats_collector=self.stats_collector,
            )
            self.delta_bundle = delta_calculator.get_delta()
            self.stats_collector.set_total_regular_files(
                self.delta_bundle.total_regular_num
            )
            logger.info(
                f"total_regular_files_num={self.delta_bundle.total_regular_num}"
            )
        except Exception as e:
            # TODO: define specific error for delta generator
            raise UpdateDeltaGenerationFailed from e

        # NOTE: now apply dirs.txt moved to here
        self.update_phase_tracker(wrapper.StatusProgressPhase.DIRECTORY)
        for entry in self.delta_bundle.new_dirs:
            entry.mkdir_relative_to_mount_point(self.standby_slot_mp)

    def _process_persistents(self):
        """NOTE: just copy from legacy mode"""
        from ..copy_tree import CopyTree

        self.update_phase_tracker(wrapper.StatusProgressPhase.PERSISTENT)
        _passwd_file = Path(cfg.PASSWD_FILE)
        _group_file = Path(cfg.GROUP_FILE)
        _copy_tree = CopyTree(
            src_passwd_file=_passwd_file,
            src_group_file=_group_file,
            dst_passwd_file=self.standby_slot_mp / _passwd_file.relative_to("/"),
            dst_group_file=self.standby_slot_mp / _group_file.relative_to("/"),
        )

        persist_inf_fname = self.metadata.persistent.file
        with open(self._recycle_folder / persist_inf_fname, "r") as f:
            for entry_line in f:
                perinf = PersistentInf(entry_line)
                if (
                    perinf.path.is_file()
                    or perinf.path.is_dir()
                    or perinf.path.is_symlink()
                ):  # NOTE: not equivalent to perinf.path.exists()
                    _copy_tree.copy_with_parents(perinf.path, self.standby_slot_mp)

    def _save_meta(self):
        """Save metadata to META_FOLDER."""
        _dst = self.standby_slot_mp / Path(self.META_FOLDER).relative_to("/")
        _dst.mkdir(parents=True, exist_ok=True)

        logger.info(f"save image meta files to {_dst}")
        for meta_f in self.metadata.get_img_metafiles():
            _src = self._recycle_folder / meta_f.file
            shutil.copy(_src, _dst)

    def _process_symlinks(self):
        symlink_inf_fname = self.metadata.symboliclink.file
        with open(self._recycle_folder / symlink_inf_fname, "r") as f:
            for entry_line in f:
                SymbolicLinkInf(entry_line).link_at_mount_point(self.standby_slot_mp)

    def _process_regulars(self):
        self.update_phase_tracker(wrapper.StatusProgressPhase.REGULAR)
        self._hardlink_register = HardlinkRegister()

        # limitation on on-going tasks
        _tasks_tracker = SimpleTasksTracker(
            max_concurrent=self.MAX_CONCURRENT_TASKS,
            title="process_regulars",
        )

        # apply delta
        logger.info("start applying delta...")
        with ThreadPoolExecutor(thread_name_prefix="create_standby_slot") as pool:
            for _hash, _regulars_set in self.delta_bundle.new_delta.items():
                # interrupt update if _tasks_tracker collects error
                if e := _tasks_tracker.last_error:
                    logger.error(f"interrupt update due to {e!r}")
                    raise e

                fut = pool.submit(
                    self._apply_reginf_set,
                    _hash,
                    _regulars_set,
                )
                _tasks_tracker.add_task(fut)
                fut.add_done_callback(_tasks_tracker.done_callback)

            logger.info(
                "all process_regulars tasks are dispatched, wait for finishing..."
            )
            _tasks_tracker.task_collect_finished()
            _tasks_tracker.wait(self.stats_collector.wait_staging)

    def _apply_reginf_set(self, _hash: str, _regs_set: RegularInfSet):
        stats_list: List[RegInfProcessedStats] = []  # for ota stats report

        _local_copy = self._recycle_folder / _hash
        _local_copy_available = _local_copy.is_file()
        _first_copy_prepared = _local_copy_available

        for is_last, entry in _regs_set.iter_entries():
            cur_stat = RegInfProcessedStats()
            _start = time.thread_time_ns()

            # prepare first copy for the hash group
            if not _local_copy_available:
                cur_stat.op = RegProcessOperation.OP_DOWNLOAD
                entry_url, compression_alg = self.metadata.get_download_url(
                    entry, base_url=self.url_base
                )
                cur_stat.errors, cur_stat.download_bytes = self._downloader.download(
                    entry_url,
                    _local_copy,
                    digest=entry.sha256hash,
                    size=entry.size,
                    proxies=self.proxies,
                    cookies=self.cookies,
                    compression_alg=compression_alg,
                )
                _local_copy_available = True

            # record the size of this entry(by query the local copy)
            cur_stat.size = _local_copy.stat().st_size

            # special treatment on /boot folder
            _mount_point = (
                self.standby_slot_mp if not entry._base == "/boot" else self.boot_dir
            )

            # prepare this entry
            # case 1: normal file
            if entry.nlink == 1:
                # at this point, cur_stat.op is set means that this entry is downloaded
                if cur_stat.op == RegProcessOperation.OP_UNSPECIFIC:
                    cur_stat.op = RegProcessOperation.OP_COPY

                if is_last:  # move the tmp entry to the dst
                    entry.move_from_src(_local_copy, dst_mount_point=_mount_point)
                else:  # copy from the tmp dir
                    entry.copy_from_src(_local_copy, dst_mount_point=_mount_point)

            # case 2: hardlink file
            else:
                # NOTE(20220523): for regulars.txt that support hardlink group,
                #   use inode to identify the hardlink group.
                #   otherwise, use hash to identify the same hardlink file.
                cur_stat.op = RegProcessOperation.OP_LINK
                _identifier = entry.sha256hash if entry.inode is None else entry.inode

                _dst = entry.make_relative_to_mount_point(_mount_point)
                _hardlink_tracker, _is_writer = self._hardlink_register.get_tracker(
                    _identifier, _dst, entry.nlink
                )

                # writer
                if _is_writer:
                    entry.copy_from_src(_local_copy, dst_mount_point=_mount_point)
                # subscriber
                else:
                    _src = _hardlink_tracker.subscribe_no_wait()
                    _src.link_to(_dst)

                if is_last:
                    _local_copy.unlink(missing_ok=True)

            # create stat
            cur_stat.elapsed_ns = time.thread_time_ns() - _start
            stats_list.append(cur_stat)

        # report the stats to the stats_collector
        # NOTE: if first_copy_available is True, unconditionally
        # remove one entry from the stats_list to maintain the correct
        # total_regular_num
        if _first_copy_prepared:
            stats_list = stats_list[1:]
        self.stats_collector.report(*stats_list)

    ###### public API methods ######
    @classmethod
    def should_erase_standby_slot(cls) -> bool:
        return True

    @classmethod
    def is_standby_as_ref(cls) -> bool:
        return False

    def create_standby_slot(self):
        try:
            self._prepare_meta_files()  # download meta and calculate
            self._cal_and_prepare_delta()  # NOTE: dirs are processed here
            self._process_regulars()
            self._process_symlinks()
            self._process_persistents()
            self._save_meta()
        except OTAError:
            raise  # if the error is already specified and wrapped, just raise again
        except DownloadFailedSpaceNotEnough:
            raise StandbySlotSpaceNotEnoughError from None
        except (ExceedMaxRetryError, ChunkStreamingError) as e:
            raise NetworkError from e
        except Exception as e:
            # TODO: cover all errors and mapping to specific OTAError type
            raise ApplyOTAUpdateFailed from e
        finally:
            shutil.rmtree(self._recycle_folder, ignore_errors=True)
