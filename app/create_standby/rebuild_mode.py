import itertools
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, wait as concurrent_futures_wait
from pathlib import Path
from threading import Semaphore
from typing import Any, Callable, ClassVar, Dict, List
from urllib.parse import urljoin

from app.create_standby.common import (
    HardlinkRegister,
    CreateRegularStatsCollector,
    RegularStats,
    RegularInfSet,
    DeltaGenerator,
    StandbySlotCreatorProtocol,
    UpdateMeta,
)
from app.configs import OTAFileCacheControl, config as cfg
from app.proxy_info import proxy_cfg
from app.downloader import Downloader
from app.update_stats import OTAUpdateStatsCollector
from app.update_phase import OtaClientUpdatePhase
from app.ota_metadata import (
    DirectoryInf,
    OtaMetadata,
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
        stats_tracker: OTAUpdateStatsCollector,
        status_updator: Callable,
    ) -> None:
        self.cookies = update_meta.cookies
        self.metadata = update_meta.metadata
        self.url_base = update_meta.url_base
        self.reference_slot = Path(update_meta.reference_slot)
        self.standby_slot = Path(update_meta.standby_slot)
        self.boot_dir = Path(update_meta.boot_dir)
        self.stats_tracker = stats_tracker
        self.status_update: Callable = status_updator

        # the location of image at the ota server root
        self.image_base_dir = self.metadata.get_rootfsdir_info()["file"]
        self.image_base_url = urljoin(update_meta.url_base, f"{self.image_base_dir}/")

        # temp storage
        self._tmp_storage = tempfile.TemporaryDirectory(dir=cfg.OTA_TMP_STORE)
        self._tmp_folder = Path(self._tmp_storage.name)

        # configure the downloader
        self._downloader = Downloader()
        proxy = proxy_cfg.get_proxy_for_local_ota()
        if proxy:
            logger.info(f"use {proxy=} for downloading")
            self._downloader.configure_proxy(proxy)

    def _prepare_meta_files(self):
        for fname, method in self.META_FILES.items():
            list_info = getattr(self.metadata, method)()
            self._downloader.download(
                path=list_info["file"],
                dst=self._tmp_folder / fname,
                digest=list_info["hash"],
                url_base=self.url_base,
                cookies=self.cookies,
                headers={
                    OTAFileCacheControl.header_lower.value: OTAFileCacheControl.no_cache.value
                },
            )

        # TODO: hardcoded regulars.txt
        delta_calculator = DeltaGenerator(
            old_reg=Path(self.META_FOLDER) / "regulars.txt",
            new_reg=self._tmp_folder / "regulars.txt",
            ref_root=self.reference_slot,
        )

        (
            self._new,
            self._hold,
            self._rm,
            self.total_files_num,
        ) = delta_calculator.calculate_delta()

    def _process_persistents(self):
        """NOTE: just copy from legacy mode"""
        from app.copy_tree import CopyTree

        self.status_update(OtaClientUpdatePhase.PERSISTENT)
        _passwd_file = Path(cfg.PASSWD_FILE)
        _group_file = Path(cfg.GROUP_FILE)
        _copy_tree = CopyTree(
            src_passwd_file=_passwd_file,
            src_group_file=_group_file,
            dst_passwd_file=self.standby_slot / _passwd_file.relative_to("/"),
            dst_group_file=self.standby_slot / _group_file.relative_to("/"),
        )

        with open(self._tmp_folder / "persistents.txt", "r") as f:
            for l in f:
                perinf = PersistentInf(l)
                if (
                    perinf.path.is_file()
                    or perinf.path.is_dir()
                    or perinf.path.is_symlink()
                ):  # NOTE: not equivalent to perinf.path.exists()
                    _copy_tree.copy_with_parents(perinf.path, self.standby_slot)

    def _process_dirs(self):
        self.status_update(OtaClientUpdatePhase.DIRECTORY)
        with open(self._tmp_folder / "dirs.txt", "r") as f:
            for l in f:
                DirectoryInf(l).mkdir2bank(self.standby_slot)

    def _save_meta(self):
        """Save metadata to META_FOLDER."""
        _dst = Path(self.META_FOLDER)
        _dst.mkdir(parents=True, exist_ok=True)

        logger.info(f"save image meta to {_dst}")
        for fname, _ in self.META_FILES.items():
            _src = self._tmp_folder / fname
            shutil.copy(_src, _dst)

    def _process_symlinks(self):
        with open(self._tmp_folder / "symlinks.txt", "r") as f:
            for l in f:
                SymbolicLinkInf(l).link_at_bank(self.standby_slot)

    def _process_regulars(self):
        self.status_update(OtaClientUpdatePhase.REGULAR)

        self._hardlink_register = HardlinkRegister()
        self._download_se = Semaphore(self.MAX_CONCURRENT_DOWNLOAD)
        _collector = CreateRegularStatsCollector(
            self.stats_tracker,
            total_regular_num=self.total_files_num,
            max_concurrency_tasks=self.MAX_CONCURRENT_TASKS,
        )
        self.stats_tracker.set("total_regular_files", self.total_files_num)

        with ThreadPoolExecutor(thread_name_prefix="create_standby_bank") as pool:
            # collect recycled files from _rm
            futs = []
            for _, _pathset in self._rm.items():
                futs.append(
                    pool.submit(
                        _pathset.collect_entries_to_be_recycled,
                        dst=self._tmp_folder,
                        root=self.reference_slot,
                    )
                )

            concurrent_futures_wait(futs)
            del futs  # cleanup

            # apply delta _hold and _new
            logger.info("start applying delta")
            pool.submit(_collector.collector)
            for _hash, _regulars_set in itertools.chain(
                self._hold.items(), self._new.items()
            ):
                _collector.acquire_se()
                pool.submit(
                    self._apply_reginf_set,
                    _hash,
                    _regulars_set,
                ).add_done_callback(_collector.callback)

            _collector.wait()
            logger.info("apply done")

    def _apply_reginf_set(
        self, _hash: str, _regs_set: RegularInfSet
    ) -> List[RegularStats]:
        stats_list = []
        skip_cleanup = _regs_set.skip_cleanup

        _first_copy = self._tmp_folder / _hash
        _first_copy_done = _first_copy.is_file()

        for is_last, entry in _regs_set.iter_entries():
            _start = time.thread_time()
            _stat = RegularStats()

            # prepare first copy for the hash group
            if not _first_copy_done:
                _collected_entry = _regs_set.entry_to_be_collected
                try:
                    if _collected_entry is None:
                        raise FileNotFoundError

                    _collected_entry.copy2dst(_first_copy, src_root=self.reference_slot)
                    _stat.op = "copy"
                except FileNotFoundError:  # fallback to download from remote
                    _stat.op = "download"
                    with self._download_se:  # limit on-going downloading
                        _stat.errors = self._downloader.download(
                            entry.path,
                            _first_copy,
                            entry.sha256hash,
                            url_base=self.image_base_url,
                            cookies=self.cookies,
                        )

                _first_copy_done = True

            # special treatment on /boot folder
            _mount_point = (
                self.standby_slot if not entry._base_root == "/boot" else self.boot_dir
            )

            # prepare this entry
            if entry.nlink == 1:
                if not _stat.op:
                    _stat.op = "copy"

                if is_last and not skip_cleanup:  # move the tmp entry to the dst
                    entry.move_from_src(_first_copy, dst_root=_mount_point)
                else:  # copy from the tmp dir
                    entry.copy_from_src(_first_copy, dst_root=_mount_point)
            else:
                # NOTE(20220523): for regulars.txt that support hardlink group,
                #   use inode to identify the hardlink group.
                #   otherwise, use hash to identify the same hardlink file.
                _identifier = entry.sha256hash if entry.inode is None else entry.inode

                _dst = entry.change_root(_mount_point)
                _hardlink_tracker, _is_writer = self._hardlink_register.get_tracker(
                    _identifier, _dst, entry.nlink
                )

                if _is_writer:
                    entry.copy_from_src(_first_copy, dst_root=_mount_point)
                else:
                    _stat.op = "link"
                    _src = _hardlink_tracker.subscribe_no_wait()
                    _src.link_to(_dst)

                if is_last and not skip_cleanup:
                    _first_copy.unlink(missing_ok=True)

            # finish up, collect stats
            _stat.elapsed = time.thread_time() - _start
            _stat.size = entry.size
            stats_list.append(_stat)

        return stats_list

    ###### public API methods ######
    @classmethod
    def should_erase_standby_slot(cls) -> bool:
        return True

    def create_standby_bank(self):
        try:
            # TODO(in the future): erase bank on create_standby_bank
            self._prepare_meta_files()  # download meta and calculate
            self._process_dirs()
            self._process_regulars()
            self._process_symlinks()
            self._process_persistents()
            self._save_meta()

        finally:
            self._tmp_storage.cleanup()  # finally cleanup
