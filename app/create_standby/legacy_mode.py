import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Semaphore
from typing import Callable
from urllib.parse import urljoin

from app.configs import OTAFileCacheControl, config as cfg
from app.copy_tree import CopyTree
from app.downloader import Downloader
from app.update_phase import OTAUpdatePhase
from app.ota_metadata import (
    DirectoryInf,
    PersistentInf,
    RegularInf,
    SymbolicLinkInf,
)
from app.proxy_info import proxy_cfg

from app.create_standby.common import (
    CreateStandbySlotExternalError,
    HardlinkRegister,
    StandbySlotCreatorProtocol,
    UpdateMeta,
)
from app.update_stats import OTAUpdateStatsCollector, RegInfProcessedStats
from app import log_util

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


# implementation
class LegacyMode(StandbySlotCreatorProtocol):
    MAX_CONCURRENT_DOWNLOAD = cfg.MAX_CONCURRENT_DOWNLOAD
    MAX_CONCURRENT_TASKS = cfg.MAX_CONCURRENT_TASKS

    def __init__(
        self,
        *,
        update_meta: UpdateMeta,
        stats_collector: OTAUpdateStatsCollector,
        update_phase_tracker: Callable,
    ) -> None:
        self.cookies = update_meta.cookies
        self.metadata = update_meta.metadata
        self.url_base = update_meta.url_base

        self.stats_collector = stats_collector
        self.update_phase_tracker: Callable = update_phase_tracker

        self.reference_slot = Path("/")
        self.standby_slot = Path(cfg.MOUNT_POINT)
        self.boot_dir = self.standby_slot / Path(cfg.BOOT_DIR).relative_to("/")

        # the location of image at the ota server root
        self.image_base_dir = self.metadata.get_rootfsdir_info()["file"]
        self.image_base_url = urljoin(update_meta.url_base, f"{self.image_base_dir}/")

        # internal used vars
        self._passwd_file = Path(cfg.PASSWD_FILE)
        self._group_file = Path(cfg.GROUP_FILE)

        # configure the downloader
        self._downloader = Downloader()
        proxy = proxy_cfg.get_proxy_for_local_ota()
        logger.info(f"configuring {proxy=}")

        if proxy:
            self._downloader.configure_proxy(proxy)

    def _process_directory(self):
        self.update_phase_tracker(OTAUpdatePhase.DIRECTORY)

        list_info = self.metadata.get_directories_info()
        with tempfile.NamedTemporaryFile(prefix=__name__) as f:
            # NOTE: do not use cache when fetching dir list
            self._downloader.download(
                list_info["file"],
                f.name,
                list_info["hash"],
                cookies=self.cookies,
                url_base=self.url_base,
                headers={
                    OTAFileCacheControl.header_lower.value: OTAFileCacheControl.no_cache.value
                },
            )

            with open(f.name, "r") as dir_txt:
                for line in dir_txt:
                    dirinf = DirectoryInf(line)
                    target_path = self.standby_slot.joinpath(
                        dirinf.path.relative_to("/")
                    )

                    target_path.mkdir(mode=dirinf.mode, parents=True, exist_ok=True)
                    os.chown(target_path, dirinf.uid, dirinf.gid)
                    os.chmod(target_path, dirinf.mode)

    def _process_symlink(self):
        self.update_phase_tracker(OTAUpdatePhase.SYMLINK)

        list_info = self.metadata.get_symboliclinks_info()
        with tempfile.NamedTemporaryFile(prefix=__name__) as f:
            # NOTE: do not use cache when fetching symlink list
            self._downloader.download(
                list_info["file"],
                f.name,
                list_info["hash"],
                url_base=self.url_base,
                cookies=self.cookies,
                headers={
                    OTAFileCacheControl.header_lower.value: OTAFileCacheControl.no_cache.value
                },
            )

            with open(f.name, "r") as symlink_txt:
                for line in symlink_txt:
                    # NOTE: symbolic link in /boot directory is not supported. We don't use it.
                    slinkf = SymbolicLinkInf(line)
                    slink = self.standby_slot.joinpath(slinkf.slink.relative_to("/"))
                    slink.symlink_to(slinkf.srcpath)
                    os.chown(slink, slinkf.uid, slinkf.gid, follow_symlinks=False)

    def _process_regular(self):
        self.update_phase_tracker(OTAUpdatePhase.REGULAR)

        list_info = self.metadata.get_regulars_info()
        with tempfile.NamedTemporaryFile(prefix=__name__) as f:
            # download the regulars.txt
            # NOTE: do not use cache when fetching regular files list
            self._downloader.download(
                list_info["file"],
                f.name,
                list_info["hash"],
                url_base=self.url_base,
                cookies=self.cookies,
                headers={
                    OTAFileCacheControl.header_lower.value: OTAFileCacheControl.no_cache.value
                },
            )

            self._create_regular_files(f.name)

    def _process_persistent(self):
        self.update_phase_tracker(OTAUpdatePhase.PERSISTENT)

        list_info = self.metadata.get_persistent_info()
        with tempfile.NamedTemporaryFile(prefix=__name__) as f:
            # NOTE: do not use cache when fetching persist files list
            self._downloader.download(
                list_info["file"],
                f.name,
                list_info["hash"],
                url_base=self.url_base,
                cookies=self.cookies,
                headers={
                    OTAFileCacheControl.header_lower.value: OTAFileCacheControl.no_cache.value
                },
            )

            copy_tree = CopyTree(
                src_passwd_file=self._passwd_file,
                src_group_file=self._group_file,
                dst_passwd_file=self.standby_slot / self._passwd_file.relative_to("/"),
                dst_group_file=self.standby_slot / self._group_file.relative_to("/"),
            )

            with open(f.name, "r") as persist_txt:
                for line in persist_txt:
                    perinf = PersistentInf(line)
                    if (
                        perinf.path.is_file()
                        or perinf.path.is_dir()
                        or perinf.path.is_symlink()
                    ):  # NOTE: not equivalent to perinf.path.exists()
                        copy_tree.copy_with_parents(perinf.path, self.standby_slot)

    def _create_regular_file(self, reginf: RegularInf, *, download_se: Semaphore):
        # thread_time for multithreading function
        # NOTE: for multithreading implementation,
        # when a thread is sleeping, the GIL will be released
        # and other thread will take the place to execute,
        # so we use time.thread_time here.
        begin_time = time.thread_time()

        processed = RegInfProcessedStats()
        if str(reginf.path).startswith("/boot"):
            dst = self.boot_dir / reginf.path.relative_to("/boot")
        else:
            dst = self.standby_slot / reginf.path.relative_to("/")

        # if is_hardlink file, get a tracker from the register
        is_hardlink = reginf.nlink >= 2
        if is_hardlink:
            # NOTE(20220523): for regulars.txt that support hardlink group,
            #   use inode to identify the hardlink group.
            #   otherwise, use hash to identify the same hardlink file.
            _identifier = reginf.sha256hash
            if reginf.inode:
                _identifier = reginf.inode

            _hardlink_tracker, _is_writer = self._hardlink_register.get_tracker(
                _identifier, reginf.path, reginf.nlink
            )

        # case 1: is hardlink and this thread is subscriber
        if is_hardlink and not _is_writer:
            # wait until the first copy is ready
            prev_reginf_path = _hardlink_tracker.subscribe()
            (self.standby_slot / prev_reginf_path.relative_to("/")).link_to(dst)
            processed.op = "link"

        # case 2: normal file or first copy of hardlink file
        else:
            try:
                if reginf.path.is_file() and reginf.verify_file(
                    src_root=self.reference_slot
                ):
                    # copy file from active bank if hash is the same
                    reginf.copy2slot(self.standby_slot, src_root=self.reference_slot)
                    processed.op = "copy"
                else:
                    # limit the concurrent downloading tasks
                    with download_se:
                        processed.errors = self._downloader.download(
                            reginf.path,
                            dst,
                            reginf.sha256hash,
                            url_base=self.image_base_url,
                            cookies=self.cookies,
                        )
                        processed.op = "download"

                # when first copy is ready(by copy or download),
                # inform the subscriber
                if is_hardlink and _is_writer:
                    _hardlink_tracker.writer_done()
            except Exception:
                if is_hardlink and _is_writer:
                    # signal all subscribers to abort

                    _hardlink_tracker.writer_on_failed()

                raise

        processed.size = dst.stat().st_size

        # set file permission as RegInf says
        os.chown(dst, reginf.uid, reginf.gid)
        os.chmod(dst, reginf.mode)

        processed.elapsed = time.thread_time() - begin_time

        self.stats_collector.report(processed)

    def _create_regular_files(self, regulars_file: str):
        logger.info("start to populate regular files...")
        # get total number of regular_files
        with open(regulars_file, "r") as f:
            total_files_num = len(f.readlines())

        # NOTE: check _OtaStatisticsStorage for available attributes
        logger.info(f"total_regular_files={total_files_num}")
        self.stats_collector.store.total_regular_files = total_files_num

        self._hardlink_register = HardlinkRegister()

        # limit the cocurrent tasks and downloading
        _download_se = Semaphore(self.MAX_CONCURRENT_DOWNLOAD)
        _concurrent_se = Semaphore(self.MAX_CONCURRENT_TASKS)

        def _release_concurrent_se():
            _concurrent_se.release()

        with open(regulars_file, "r") as f, ThreadPoolExecutor() as pool:
            for l in f:
                entry = RegularInf.parse_reginf(l)
                _concurrent_se.acquire()

                pool.submit(
                    self._create_regular_file,
                    entry,
                    download_se=_download_se,
                ).add_done_callback(_release_concurrent_se)

            logger.info("all create_regular_files tasks dispatched, wait for collector")

    ###### public API methods ######
    @classmethod
    def should_erase_standby_slot(cls) -> bool:
        return True

    def create_standby_bank(self):
        """Exposed API for ota-client."""
        try:
            self._process_directory()
            self._process_symlink()
            self._process_regular()
            self._process_persistent()
        except Exception as e:
            raise CreateStandbySlotExternalError from e
