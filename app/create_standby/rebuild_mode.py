from itertools import chain
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Semaphore
from typing import Callable, ClassVar, Dict, List
from urllib.parse import urljoin

from app.create_standby.common import (
    CreateStandbySlotExternalError,
    HardlinkRegister,
    RegularInfSet,
    DeltaGenerator,
    StandbySlotCreatorProtocol,
    UpdateMeta,
)
from app.configs import OTAFileCacheControl, config as cfg
from app.proxy_info import proxy_cfg
from app.downloader import Downloader
from app.update_stats import OTAUpdateStatsCollector, RegInfProcessedStats
from app.update_phase import OTAUpdatePhase
from app.ota_metadata import (
    DirectoryInf,
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
        self.reference_slot = Path("/")
        self.standby_slot = Path(cfg.MOUNT_POINT)

        # the location of image at the ota server root
        self.image_base_dir = self.metadata.get_rootfsdir_info()["file"]
        self.image_base_url = urljoin(update_meta.url_base, f"{self.image_base_dir}/")

        # recycle folder
        self._recycle_folder = Path(cfg.OTA_TMP_STORE)
        self._recycle_folder.mkdir()

        # configure the downloader
        self._downloader = Downloader()
        proxy = proxy_cfg.get_proxy_for_local_ota()
        if proxy:
            logger.info(f"use {proxy=} for downloading")
            self._downloader.configure_proxy(proxy)

    def _prepare_meta_files(self):
        self.update_phase_tracker(OTAUpdatePhase.METADATA)
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

    def _process_persistents(self):
        """NOTE: just copy from legacy mode"""
        from app.copy_tree import CopyTree

        self.update_phase_tracker(OTAUpdatePhase.PERSISTENT)
        _passwd_file = Path(cfg.PASSWD_FILE)
        _group_file = Path(cfg.GROUP_FILE)
        _copy_tree = CopyTree(
            src_passwd_file=_passwd_file,
            src_group_file=_group_file,
            dst_passwd_file=self.standby_slot / _passwd_file.relative_to("/"),
            dst_group_file=self.standby_slot / _group_file.relative_to("/"),
        )

        with open(self._recycle_folder / "persistents.txt", "r") as f:
            for l in f:
                perinf = PersistentInf(l)
                if (
                    perinf.path.is_file()
                    or perinf.path.is_dir()
                    or perinf.path.is_symlink()
                ):  # NOTE: not equivalent to perinf.path.exists()
                    _copy_tree.copy_with_parents(perinf.path, self.standby_slot)

    def _process_dirs(self):
        self.update_phase_tracker(OTAUpdatePhase.DIRECTORY)
        with open(self._recycle_folder / "dirs.txt", "r") as f:
            for l in f:
                DirectoryInf(l).mkdir2bank(self.standby_slot)

    def _save_meta(self):
        """Save metadata to META_FOLDER."""
        _dst = Path(self.META_FOLDER)
        _dst.mkdir(parents=True, exist_ok=True)

        logger.info(f"save image meta to {_dst}")
        for fname, _ in self.META_FILES.items():
            _src = self._recycle_folder / fname
            shutil.copy(_src, _dst)

    def _process_symlinks(self):
        with open(self._recycle_folder / "symlinks.txt", "r") as f:
            for l in f:
                SymbolicLinkInf(l).link_at_bank(self.standby_slot)

    def _process_regulars(self):
        self.update_phase_tracker(OTAUpdatePhase.REGULAR)

        # TODO: hardcoded regulars.txt
        # TODO 2: old_reg is not used currently
        logger.info("generating delta...")
        delta_calculator = DeltaGenerator(
            old_reg=Path(self.META_FOLDER) / "regulars.txt",
            new_reg=self._recycle_folder / "regulars.txt",
            ref_root=self.reference_slot,
            recycle_folder=self._recycle_folder,
        )
        delta_bundle = delta_calculator.get_delta()
        self.stats_collector.store.total_regular_files = delta_bundle.total_regular_num
        logger.info(f"total_regular_files_num={delta_bundle.total_regular_num}")

        self._hardlink_register = HardlinkRegister()

        # limitation on on-going tasks/download
        _download_se = Semaphore(self.MAX_CONCURRENT_DOWNLOAD)
        _concurrent_se = Semaphore(self.MAX_CONCURRENT_TASKS)

        def _release_concurrent_se():
            _concurrent_se.release()

        # apply delta
        with ThreadPoolExecutor(thread_name_prefix="create_standby_bank") as pool:
            logger.info("start applying delta")
            for _hash, _regulars_set in chain(
                delta_bundle._hold.items(), delta_bundle._new.items()
            ):
                _concurrent_se.acquire()
                pool.submit(
                    self._apply_reginf_set,
                    _hash,
                    _regulars_set,
                    download_se=_download_se,
                ).add_done_callback(_release_concurrent_se)

            logger.info("delta apply done")

    def _apply_reginf_set(
        self, _hash: str, _regs_set: RegularInfSet, *, download_se: Semaphore
    ):
        stats_list: List[RegInfProcessedStats] = []  # for ota stats report

        _local_copy = self._recycle_folder / _hash
        _local_copy_available = _local_copy.is_file()
        _first_copy_prepared = _local_copy_available

        for is_last, entry in _regs_set.iter_entries():
            _start = time.thread_time()
            _op, _errors = "copy", 0

            # prepare first copy for the hash group
            if not _local_copy_available:
                _op = "download"
                with download_se:  # limit on-going downloading
                    _errors = self._downloader.download(
                        entry.path,
                        _local_copy,
                        entry.sha256hash,
                        url_base=self.image_base_url,
                        cookies=self.cookies,
                    )

                _local_copy_available = True

            # special treatment on /boot folder
            _mount_point = (
                self.standby_slot if not entry._base == "/boot" else self.boot_dir
            )

            # prepare this entry
            if entry.nlink == 1:
                if is_last:  # move the tmp entry to the dst
                    entry.move_from_src(_local_copy, dst_root=_mount_point)
                else:  # copy from the tmp dir
                    entry.copy_from_src(_local_copy, dst_root=_mount_point)
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
                    entry.copy_from_src(_local_copy, dst_root=_mount_point)
                else:
                    _op = "link"
                    _src = _hardlink_tracker.subscribe_no_wait()
                    _src.link_to(_dst)

                if is_last:
                    _local_copy.unlink(missing_ok=True)

            stats_list.append(
                RegInfProcessedStats(
                    op=_op,
                    size=entry.size,
                    elapsed=time.thread_time() - _start,
                    errors=_errors,
                )
            )

        # NOTE: exclude one entry as we prepare local copy
        # when we are geernating delta
        if _first_copy_prepared:
            stats_list = stats_list[1:]
        self.stats_collector.report(*stats_list)

    ###### public API methods ######
    @classmethod
    def should_erase_standby_slot(cls) -> bool:
        return True

    def create_standby_bank(self):
        try:
            self._prepare_meta_files()  # download meta and calculate
            self._process_dirs()
            self._process_regulars()
            self._process_symlinks()
            self._process_persistents()
            self._save_meta()
        except Exception as e:
            raise CreateStandbySlotExternalError from e
        finally:
            shutil.rmtree(self._recycle_folder, ignore_errors=True)
