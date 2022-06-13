import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Semaphore
from typing import Any, Callable, Dict, List
from urllib.parse import urljoin

from app.configs import OTAFileCacheControl, config as cfg
from app.copy_tree import CopyTree
from app.downloader import Downloader
from app.update_stats import OTAUpdateStatsCollector
from app.update_phase import OtaClientUpdatePhase
from app.ota_metadata import (
    DirectoryInf,
    OtaMetadata,
    PersistentInf,
    RegularInf,
    SymbolicLinkInf,
)
from app.proxy_info import proxy_cfg

from app.create_standby.common import (
    HardlinkRegister,
    RegularStats,
    CreateRegularStatsCollector,
    StandbySlotCreatorProtocol,
    UpdateMeta,
)

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
        stats_tracker: OTAUpdateStatsCollector,
        status_updator: Callable,
    ) -> None:
        self.cookies = update_meta.cookies
        self.metadata = update_meta.metadata
        self.url_base = update_meta.url_base
        self.standby_slot = Path(update_meta.standby_slot)
        self.boot_dir = Path(update_meta.boot_dir)
        self.reference_slot = Path(update_meta.reference_slot)
        self.stats_tracker = stats_tracker
        self.status_update: Callable = status_updator

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

    def _create_regular_file(self, reginf: RegularInf) -> List[RegularStats]:
        # thread_time for multithreading function
        # NOTE: for multithreading implementation,
        # when a thread is sleeping, the GIL will be released
        # and other thread will take the place to execute,
        # so we use time.thread_time here.
        begin_time = time.thread_time()

        processed = RegularStats()
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
                    reginf.copy2bank(self.standby_slot, src_root=self.reference_slot)
                    processed.op = "copy"
                else:
                    # limit the concurrent downloading tasks
                    with self._download_se:
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

        return [processed]

    def _create_regular_files(self, regulars_file: str):
        # get total number of regular_files
        with open(regulars_file, "r") as f:
            total_files_num = len(f.readlines())

        # NOTE: check _OtaStatisticsStorage for available attributes
        self.stats_tracker.set("total_regular_files", total_files_num)

        self._hardlink_register = HardlinkRegister()
        # collector that records stats from tasks and update ota-update status
        _collector = CreateRegularStatsCollector(
            self.stats_tracker,
            total_regular_num=total_files_num,
            max_concurrency_tasks=self.MAX_CONCURRENT_TASKS,
        )
        # limit the cocurrent downloading tasks
        self._download_se = Semaphore(self.MAX_CONCURRENT_DOWNLOAD)

        with open(regulars_file, "r") as f, ThreadPoolExecutor() as pool:
            # fire up background collector
            pool.submit(_collector.collector)

            for l in f:
                entry = RegularInf(l)
                _collector.acquire_se()

                fut = pool.submit(
                    self._create_regular_file,
                    entry,
                )
                fut.add_done_callback(_collector.callback)

            logger.info("all create_regular_files tasks dispatched, wait for collector")

            # wait for collector
            _collector.wait()

    def create_standby_bank(self):
        """Exposed API for ota-client."""
        self.status_update(OtaClientUpdatePhase.DIRECTORY)
        self._process_directory()

        self.status_update(OtaClientUpdatePhase.SYMLINK)
        self._process_symlink()

        self.status_update(OtaClientUpdatePhase.REGULAR)
        self._process_regular()

        self.status_update(OtaClientUpdatePhase.PERSISTENT)
        self._process_persistent()
