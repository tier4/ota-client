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


import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, List, Set, Tuple

from ..common import RetryTaskMap
from ..configs import config as cfg
from ..ota_metadata import MetafilesV1
from ..update_stats import (
    OTAUpdateStatsCollector,
    RegInfProcessedStats,
    RegProcessOperation,
)
from ..proto.wrapper import RegularInf, StatusProgressPhase
from .. import log_setting

from .common import HardlinkRegister, DeltaGenerator, DeltaBundle
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
        update_phase_tracker: Callable[[StatusProgressPhase], None],
    ) -> None:
        self._ota_metadata = update_meta.metadata
        self.stats_collector = stats_collector
        self.update_phase_tracker = update_phase_tracker

        # path configuration
        self.boot_dir = Path(update_meta.boot_dir)
        self.standby_slot_mp = Path(update_meta.standby_slot_mp)
        self.active_slot_mp = Path(update_meta.active_slot_mp)

        # recycle folder, files copied from referenced slot will be stored here,
        # also the meta files will be stored under this folder
        self._ota_tmp = self.standby_slot_mp / Path(cfg.OTA_TMP_STORE).relative_to("/")
        self._ota_tmp.mkdir(exist_ok=True)

    def _cal_and_prepare_delta(self):
        logger.info("generating delta...")
        delta_calculator = DeltaGenerator(
            ota_metadata=self._ota_metadata,
            delta_src=self.active_slot_mp,
            local_copy_dir=self._ota_tmp,
            stats_collector=self.stats_collector,
        )
        delta_bundle = delta_calculator.calculate_and_process_delta()
        self.stats_collector.set_total_regular_files(delta_bundle.total_regular_num)
        logger.info(f"total_regular_files_num={delta_bundle.total_regular_num}")
        self.delta_bundle = delta_bundle

    def _process_dirs(self):
        self.update_phase_tracker(StatusProgressPhase.DIRECTORY)
        for entry in self.delta_bundle.get_new_dirs():
            entry.mkdir_relative_to_mount_point(self.standby_slot_mp)

    def _process_persistents(self):
        """NOTE: just copy from legacy mode"""
        from ..copy_tree import CopyTree

        self.update_phase_tracker(StatusProgressPhase.PERSISTENT)
        _passwd_file = Path(cfg.PASSWD_FILE)
        _group_file = Path(cfg.GROUP_FILE)
        _copy_tree = CopyTree(
            src_passwd_file=_passwd_file,
            src_group_file=_group_file,
            dst_passwd_file=self.standby_slot_mp / _passwd_file.relative_to("/"),
            dst_group_file=self.standby_slot_mp / _group_file.relative_to("/"),
        )

        for _perinf in self._ota_metadata.iter_metafile(MetafilesV1.PERSISTENTS):
            _perinf_path = Path(_perinf.path)
            if (
                _perinf_path.is_file()
                or _perinf_path.is_dir()
                or _perinf_path.is_symlink()
            ):  # NOTE: not equivalent to perinf.path.exists()
                _copy_tree.copy_with_parents(_perinf_path, self.standby_slot_mp)

    def _process_symlinks(self):
        self.update_phase_tracker(StatusProgressPhase.SYMLINK)
        for _symlink in self._ota_metadata.iter_metafile(MetafilesV1.SYMLINKS):
            _symlink.link_at_mount_point(self.standby_slot_mp)

    def _process_regulars(self):
        self.update_phase_tracker(StatusProgressPhase.REGULAR)
        self._hardlink_register = HardlinkRegister()

        logger.info("start applying delta...")
        with ThreadPoolExecutor(thread_name_prefix="create_standby_slot") as pool:
            _mapper = RetryTaskMap(
                self._process_regular,
                self.delta_bundle.new_delta.items(),
                title="process_regulars",
                max_concurrent=cfg.MAX_CONCURRENT_PROCESS_FILE_TASKS,
                backoff_max=cfg.CREATE_STANDBY_BACKOFF_MAX,
                backoff_factor=cfg.CREATE_STANDBY_BACKOFF_FACTOR,
                max_retry=cfg.CREATE_STANDBY_RETRY_MAX,
                executor=pool,
            )
            for _exp, _entry, _ in _mapper.execute():
                if _exp:
                    logger.debug(f"[process_regular] failed to process {_entry=}")
            self.stats_collector.wait_staging()

    def _process_regular(self, _input: Tuple[bytes, Set[RegularInf]]):
        _hash, _regs_set = _input
        _hash_str = _hash.hex()
        stats_list: List[RegInfProcessedStats] = []  # for ota stats report

        _local_copy = self._ota_tmp / _hash_str
        for _count, entry in enumerate(_regs_set, start=1):
            is_last = _count == len(_regs_set)

            cur_stat = RegInfProcessedStats(
                op=RegProcessOperation.OP_COPY,
                size=_local_copy.stat().st_size,
            )
            _start_time = time.thread_time_ns()

            # special treatment on /boot folder
            _mount_point = (
                self.standby_slot_mp
                if not entry.path.startswith("/boot")
                else self.boot_dir
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
                _identifier = entry.sha256hash if not entry.inode else entry.inode

                _dst = entry.relatively_join(_mount_point)
                _hardlink_tracker, _is_writer = self._hardlink_register.get_tracker(
                    _identifier, _dst, entry.nlink
                )

                if _is_writer:
                    entry.copy_from_src(_local_copy, dst_mount_point=_mount_point)
                else:  # subscriber
                    _src = _hardlink_tracker.subscribe_no_wait()
                    os.link(_src, _dst)

                if is_last:
                    _local_copy.unlink(missing_ok=True)

            # create stat
            cur_stat.elapsed_ns = time.thread_time_ns() - _start_time
            stats_list.append(cur_stat)

        # report the stats to the stats_collector
        # NOTE: unconditionally pop one stat from the stats_list
        #       because the preparation of first copy is already recorded
        #       (either by picking up local copy or downloading)
        self.stats_collector.report(*stats_list[1:])

    def _save_meta(self):
        """Save metadata to META_FOLDER."""
        _dst = self.standby_slot_mp / Path(cfg.META_FOLDER).relative_to("/")
        _dst.mkdir(parents=True, exist_ok=True)

        logger.info(f"save image meta files to {_dst}")
        self._ota_metadata.save_metafiles_bin_to(_dst)

    # API

    @classmethod
    def should_erase_standby_slot(cls) -> bool:
        return True

    def calculate_and_prepare_delta(self) -> DeltaBundle:
        self._cal_and_prepare_delta()
        return self.delta_bundle

    def create_standby_slot(self):
        """Apply changes to the standby slot.

        This method should be called before calculate_and_prepare_delta.
        """
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
