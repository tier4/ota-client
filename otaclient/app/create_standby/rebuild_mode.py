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
from ..common import (
    SimpleTasksTracker,
    OTAFileCacheControl,
    urljoin_ensure_base,
    RetryTaskMap,
)
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
    RegularInf,
    SymbolicLinkInf,
)
from .. import log_setting

from .common import HardlinkRegister, RegularInfSet, DeltaGenerator, DeltaBundle
from .interface import StandbySlotCreatorProtocol, UpdateMeta

logger = log_setting.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class RebuildMode(StandbySlotCreatorProtocol):
    def __init__(
        self,
        *,
        update_meta: UpdateMeta,
        stats_collector: OTAUpdateStatsCollector,
        update_phase_tracker: Callable[[wrapper.StatusProgressPhase], None],
        downloader: Downloader,
    ) -> None:
        self._cookies = update_meta.cookies
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
        self._ota_tmp = self.standby_slot_mp / Path(cfg.OTA_TMP_STORE).relative_to("/")
        # TODO: write this to conf
        self._ota_tmp_image_meta_dir = Path(cfg.MOUNT_POINT) / ".ota_metafiles"
        self._ota_tmp.mkdir(exist_ok=True)
        # downloaded otameta files are stored under this folder
        self._ota_tmp_image_meta_dir.mkdir(exist_ok=True)

        # configure the downloader
        self._downloader = downloader
        self._proxies = None
        if proxy := proxy_cfg.get_proxy_for_local_ota():
            logger.info(f"use {proxy=} for downloading")
            # NOTE: check requests doc for details
            self._proxies = {"http": proxy}

    def _cal_and_prepare_delta(self):
        logger.info("generating delta...")

        regular_inf_fname = self.metadata.regular.file
        dir_inf_fname = self.metadata.directory.file
        try:
            delta_calculator = DeltaGenerator(
                delta_src_reg=Path(cfg.META_FOLDER) / regular_inf_fname,
                new_reg=self._ota_tmp_image_meta_dir / regular_inf_fname,
                new_dirs=self._ota_tmp_image_meta_dir / dir_inf_fname,
                delta_src=self.reference_slot_mp,
                local_copy_dir=self._ota_tmp,
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

    def _process_dirs(self):
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
        with open(self._ota_tmp_image_meta_dir / persist_inf_fname, "r") as f:
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
        _dst = self.standby_slot_mp / Path(cfg.META_FOLDER).relative_to("/")
        _dst.mkdir(parents=True, exist_ok=True)

        logger.info(f"save image meta files to {_dst}")
        for meta_f in self.metadata.get_img_metafiles():
            _src = self._ota_tmp_image_meta_dir / meta_f.file
            shutil.copy(_src, _dst)

    def _process_symlinks(self):
        self.update_phase_tracker(wrapper.StatusProgressPhase.SYMLINK)
        symlink_inf_fname = self.metadata.symboliclink.file
        with open(self._ota_tmp_image_meta_dir / symlink_inf_fname, "r") as f:
            for entry_line in f:
                SymbolicLinkInf(entry_line).link_at_mount_point(self.standby_slot_mp)

    def _process_regulars(self):
        self.update_phase_tracker(wrapper.StatusProgressPhase.REGULAR)
        self._hardlink_register = HardlinkRegister()
        _tasks_tracker = SimpleTasksTracker(
            max_concurrent=cfg.MAX_CONCURRENT_TASKS,
            title="process_regulars",
        )

        logger.info("start applying delta...")
        with ThreadPoolExecutor(thread_name_prefix="create_standby_slot") as pool:
            for _hash, _regulars_set in self.delta_bundle.new_delta.items():
                # interrupt update if _tasks_tracker collects error
                if e := _tasks_tracker.last_error:
                    logger.error(f"interrupt update due to {e!r}")
                    raise e

                fut = pool.submit(
                    self._process_regular,
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

    def _process_regular(self, _hash: str, _regs_set: RegularInfSet):
        stats_list: List[RegInfProcessedStats] = []  # for ota stats report

        _local_copy = self._ota_tmp / _hash
        for is_last, entry in _regs_set.iter_entries():
            cur_stat = RegInfProcessedStats(op=RegProcessOperation.OP_COPY)
            _start = time.thread_time_ns()

            # record the size of this entry(by query the local copy)
            cur_stat.size = _local_copy.stat().st_size
            # special treatment on /boot folder
            _mount_point = (
                self.standby_slot_mp if not entry._base == "/boot" else self.boot_dir
            )

            # prepare this entry
            # case 1: normal file
            if entry.nlink == 1:
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
        # NOTE: unconditionally pop one stat from the stats_list
        #       because the preparation of first copy is already recorded
        #       (either by picking up local copy or downloading)
        self.stats_collector.report(*stats_list[1:])

    ###### public API methods ######
    @classmethod
    def should_erase_standby_slot(cls) -> bool:
        return True

    @classmethod
    def is_standby_as_ref(cls) -> bool:
        return False

    def calculate_and_prepare_delta(self) -> DeltaBundle:
        try:
            self._cal_and_prepare_delta()
            return self.delta_bundle
        except OTAError:
            raise  # if the error is already specified and wrapped, just raise again
        except Exception as e:
            # TODO: cover all errors and mapping to specific OTAError type
            raise ApplyOTAUpdateFailed from e

    def create_standby_slot(self):
        """Apply changes to the standby slot.

        This method should be called before calculate_and_prepare_delta.
        """
        try:
            self._process_dirs()
            self._process_regulars()
            self._process_symlinks()
            self._process_persistents()
            self._save_meta()
            # cleanup on successful finish
            # NOTE: if failed, the tmp dirs will not be removed now,
            #       but rebuild mode will not reuse the tmp dirs anyway
            #       as it requires standby_slot to be erased before applying
            #       the changes.
            shutil.rmtree(self._ota_tmp, ignore_errors=True)
            shutil.rmtree(self._ota_tmp_image_meta_dir, ignore_errors=True)
        except OTAError:
            raise  # if the error is already specified and wrapped, just raise again
        except Exception as e:
            # TODO: cover all errors and mapping to specific OTAError type
            raise ApplyOTAUpdateFailed from e
