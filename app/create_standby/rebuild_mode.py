import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Semaphore
from typing import Callable, ClassVar, Dict, List
from urllib.parse import urljoin
from app.errors import NetworkError, OTAError, StandbySlotSpaceNotEnoughError

from app.common import SimpleTasksTracker, OTAFileCacheControl
from app.create_standby.common import HardlinkRegister, RegularInfSet, DeltaGenerator
from app.create_standby.interface import StandbySlotCreatorProtocol, UpdateMeta
from app.configs import config as cfg
from app.errors import (
    ApplyOTAUpdateFailed,
    OTAErrorUnRecoverable,
    OTAMetaDownloadFailed,
    OTAMetaVerificationFailed,
    UpdateDeltaGenerationFailed,
)
from app.proxy_info import proxy_cfg
from app.downloader import (
    ChunkStreamingError,
    DestinationNotAvailableError,
    Downloader,
    ExceedMaxRetryError,
    HashVerificaitonError,
    DownloadFailedSpaceNotEnough,
)
from app.update_stats import (
    OTAUpdateStatsCollector,
    RegInfProcessedStats,
    RegProcessOperation,
)
from app.update_phase import OTAUpdatePhase
from app.ota_metadata import (
    PersistentInf,
    SymbolicLinkInf,
)

from app import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class RebuildMode(StandbySlotCreatorProtocol):
    MAX_CONCURRENT_DOWNLOAD = cfg.MAX_CONCURRENT_DOWNLOAD
    MAX_CONCURRENT_TASKS = cfg.MAX_CONCURRENT_TASKS
    OTA_TMP_STORE = cfg.OTA_TMP_STORE
    META_FOLDER = cfg.META_FOLDER
    META_FILES: ClassVar[Dict[str, str]] = {
        "dirs.txt": "get_directories_info",
        "regulars.txt": "get_regulars_info",
        "persistents.txt": "get_persistent_info",
        "symlinks.txt": "get_symboliclinks_info",
    }

    def __init__(
        self,
        *,
        update_meta: UpdateMeta,
        stats_collector: OTAUpdateStatsCollector,
        update_phase_tracker: Callable[[OTAUpdatePhase], None],
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

        # the location of image at the ota server root
        self.image_base_dir = self.metadata.get_rootfsdir_info()["file"]
        self.image_base_url = urljoin(update_meta.url_base, f"{self.image_base_dir}/")

        # recycle folder, files copied from referenced slot will be stored here,
        # also the meta files will be stored under this folder
        self._recycle_folder = self.standby_slot_mp / Path(
            cfg.OTA_TMP_STORE
        ).relative_to("/")
        self._recycle_folder.mkdir()

        # configure the downloader
        self._downloader = Downloader()
        proxy = proxy_cfg.get_proxy_for_local_ota()
        if proxy:
            logger.info(f"use {proxy=} for downloading")
            self._downloader.configure_proxy(proxy)

    def _prepare_meta_files(self):
        self.update_phase_tracker(OTAUpdatePhase.METADATA)
        try:
            for fname, method in self.META_FILES.items():
                list_info = getattr(self.metadata, method)()
                self._downloader.download(
                    path=list_info["file"],
                    dst=self._recycle_folder / fname,
                    digest=list_info["hash"],
                    url_base=self.url_base,
                    cookies=self.cookies,
                    headers={
                        OTAFileCacheControl.header_lower.value: OTAFileCacheControl.no_cache.value
                    },
                )
        except (ExceedMaxRetryError, ChunkStreamingError) as e:
            raise OTAMetaDownloadFailed from e
        except HashVerificaitonError as e:
            raise OTAMetaVerificationFailed from e
        except DestinationNotAvailableError as e:
            raise OTAErrorUnRecoverable from e

    def _cal_and_prepare_delta(self):
        self.update_phase_tracker(OTAUpdatePhase.REGULAR)

        # TODO: hardcoded regulars.txt
        # TODO 2: old_reg is not used currently
        logger.info("generating delta...")

        try:
            delta_calculator = DeltaGenerator(
                old_reg=Path(self.META_FOLDER) / "regulars.txt",
                new_reg=self._recycle_folder / "regulars.txt",
                new_dirs=self._recycle_folder / "dirs.txt",
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
        self.update_phase_tracker(OTAUpdatePhase.DIRECTORY)
        for entry in self.delta_bundle.new_dirs:
            entry.mkdir_relative_to_mount_point(self.standby_slot_mp)

    def _process_persistents(self):
        """NOTE: just copy from legacy mode"""
        from app.copy_tree import CopyTree

        self.update_phase_tracker(OTAUpdatePhase.PERSISTENT)
        _passwd_file = Path(cfg.PASSWD_FILE)
        _group_file = Path(cfg.GROUP_FILE)
        _copy_tree = CopyTree(
            src_passwd_file=_passwd_file,
            src_group_file=_group_file,
            dst_passwd_file=self.standby_slot_mp / _passwd_file.relative_to("/"),
            dst_group_file=self.standby_slot_mp / _group_file.relative_to("/"),
        )

        with open(self._recycle_folder / "persistents.txt", "r") as f:
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
        for fname, _ in self.META_FILES.items():
            _src = self._recycle_folder / fname
            shutil.copy(_src, _dst)

    def _process_symlinks(self):
        with open(self._recycle_folder / "symlinks.txt", "r") as f:
            for entry_line in f:
                SymbolicLinkInf(entry_line).link_at_mount_point(self.standby_slot_mp)

    def _process_regulars(self):
        self.update_phase_tracker(OTAUpdatePhase.REGULAR)
        self._hardlink_register = HardlinkRegister()

        # limitation on on-going tasks/download
        _download_se = Semaphore(self.MAX_CONCURRENT_DOWNLOAD)
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
                    download_se=_download_se,
                )
                _tasks_tracker.add_task(fut)
                fut.add_done_callback(_tasks_tracker.done_callback)

            logger.info(
                "all process_regulars tasks are dispatched, wait for finishing..."
            )
            _tasks_tracker.task_collect_finished()
            _tasks_tracker.wait(self.stats_collector.wait_staging)

    def _apply_reginf_set(
        self, _hash: str, _regs_set: RegularInfSet, *, download_se: Semaphore
    ):
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
                with download_se:  # limit on-going downloading
                    cur_stat.errors = self._downloader.download(
                        entry.path,
                        _local_copy,
                        entry.sha256hash,
                        url_base=self.image_base_url,
                        cookies=self.cookies,
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

            # do a os.sync after the standby slot creation finished
            os.sync()
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
