import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Semaphore
from typing import Callable
from urllib.parse import urljoin
from app.errors import ApplyOTAUpdateFailed, OTAError, StandbySlotSpaceNotEnoughError

from app.common import SimpleTasksTracker, OTAFileCacheControl
from app.configs import config as cfg
from app.copy_tree import CopyTree
from app.downloader import (
    ChunkStreamingError,
    DownloadFailedSpaceNotEnough,
    Downloader,
    ExceedMaxRetryError,
)
from app.errors import NetworkError
from app.ota_metadata import (
    DirectoryInf,
    PersistentInf,
    RegularInf,
    SymbolicLinkInf,
)
from app.proto import wrapper
from app.proxy_info import proxy_cfg

from app.create_standby.common import HardlinkRegister
from app.create_standby.interface import StandbySlotCreatorProtocol, UpdateMeta
from app.update_stats import (
    OTAUpdateStatsCollector,
    RegInfProcessedStats,
    RegProcessOperation,
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
        stats_collector: OTAUpdateStatsCollector,
        update_phase_tracker: Callable,
    ) -> None:
        self.cookies = update_meta.cookies
        self.metadata = update_meta.metadata
        self.url_base = update_meta.url_base

        self.stats_collector = stats_collector
        self.update_phase_tracker: Callable = update_phase_tracker

        self.reference_slot_mp = Path(update_meta.ref_slot_mount_point)
        self.standby_slot_mp = Path(update_meta.standby_slot_mount_point)
        self.boot_dir = Path(update_meta.boot_dir)

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
        self.update_phase_tracker(wrapper.StatusProgressPhase.DIRECTORY)

        list_info = self.metadata.get_directories_info()
        with tempfile.NamedTemporaryFile(prefix=__name__) as f:
            # NOTE: do not use cache when fetching dir list
            self._downloader.download(
                list_info["file"],
                Path(f.name),
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
                    target_path = self.standby_slot_mp.joinpath(
                        dirinf.path.relative_to("/")
                    )

                    target_path.mkdir(mode=dirinf.mode, parents=True, exist_ok=True)
                    os.chown(target_path, dirinf.uid, dirinf.gid)
                    os.chmod(target_path, dirinf.mode)

    def _process_symlink(self):
        self.update_phase_tracker(wrapper.StatusProgressPhase.SYMLINK)

        list_info = self.metadata.get_symboliclinks_info()
        with tempfile.NamedTemporaryFile(prefix=__name__) as f:
            # NOTE: do not use cache when fetching symlink list
            self._downloader.download(
                list_info["file"],
                Path(f.name),
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
                    slinkf.link_at_mount_point(self.standby_slot_mp)

    def _process_regular(self):
        self.update_phase_tracker(wrapper.StatusProgressPhase.REGULAR)

        list_info = self.metadata.get_regulars_info()
        with tempfile.NamedTemporaryFile(prefix=__name__) as f:
            # download the regulars.txt
            # NOTE: do not use cache when fetching regular files list
            self._downloader.download(
                list_info["file"],
                Path(f.name),
                list_info["hash"],
                url_base=self.url_base,
                cookies=self.cookies,
                headers={
                    OTAFileCacheControl.header_lower.value: OTAFileCacheControl.no_cache.value
                },
            )

            self._create_regular_files(f.name)

    def _process_persistent(self):
        self.update_phase_tracker(wrapper.StatusProgressPhase.PERSISTENT)

        list_info = self.metadata.get_persistent_info()
        with tempfile.NamedTemporaryFile(prefix=__name__) as f:
            # NOTE: do not use cache when fetching persist files list
            self._downloader.download(
                list_info["file"],
                Path(f.name),
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
                dst_passwd_file=self.standby_slot_mp
                / self._passwd_file.relative_to("/"),
                dst_group_file=self.standby_slot_mp / self._group_file.relative_to("/"),
            )

            with open(f.name, "r") as persist_txt:
                for line in persist_txt:
                    perinf = PersistentInf(line)
                    if (
                        perinf.path.is_file()
                        or perinf.path.is_dir()
                        or perinf.path.is_symlink()
                    ):  # NOTE: not equivalent to perinf.path.exists()
                        copy_tree.copy_with_parents(perinf.path, self.standby_slot_mp)

    def _create_regular_file(self, reginf: RegularInf, *, download_se: Semaphore):
        # thread_time for multithreading function
        # NOTE: for multithreading implementation,
        # when a thread is sleeping, the GIL will be released
        # and other thread will take the place to execute,
        # so we use time.thread_time here.
        begin_time = time.thread_time_ns()

        processed = RegInfProcessedStats()
        if str(reginf.path).startswith("/boot"):
            dst = self.boot_dir / reginf.path.relative_to("/boot")
        else:
            dst = self.standby_slot_mp / reginf.path.relative_to("/")

        # case 1: if is_hardlink file, get a tracker from the register
        if reginf.nlink >= 2:
            # NOTE(20220523): for regulars.txt that support hardlink group,
            #   use inode to identify the hardlink group.
            #   otherwise, use hash to identify the same hardlink file.
            _identifier = reginf.sha256hash
            if reginf.inode:
                _identifier = reginf.inode

            _hardlink_tracker, _is_writer = self._hardlink_register.get_tracker(
                _identifier, reginf.path, reginf.nlink
            )

            # case 1.1: is hardlink and this thread is subscriber
            if not _is_writer:
                # wait until the first copy is ready
                prev_reginf_path = _hardlink_tracker.subscribe()
                (self.standby_slot_mp / prev_reginf_path.relative_to("/")).link_to(dst)
                processed.op = RegProcessOperation.OP_LINK

            # case 1.2: is hardlink and is writer
            else:
                try:
                    if reginf.path.is_file() and reginf.verify_file(
                        src_mount_point=self.reference_slot_mp
                    ):
                        # copy file from active bank if hash is the same
                        reginf.copy_relative_to_mount_point(
                            self.standby_slot_mp, src_mount_point=self.reference_slot_mp
                        )
                        processed.op = RegProcessOperation.OP_COPY
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
                            processed.op = RegProcessOperation.OP_DOWNLOAD

                        # set file permission as RegInf says
                        os.chown(dst, reginf.uid, reginf.gid)
                        os.chmod(dst, reginf.mode)

                    # when first copy is ready(by copy or download),
                    # inform the subscriber
                    _hardlink_tracker.writer_done()
                except Exception:
                    # signal all subscribers to abort
                    _hardlink_tracker.writer_on_failed()

                    raise

        # case 2: normal file
        else:
            if reginf.path.is_file() and reginf.verify_file(
                src_mount_point=self.reference_slot_mp
            ):
                # copy file from active bank if hash is the same
                reginf.copy_relative_to_mount_point(
                    self.standby_slot_mp, src_mount_point=self.reference_slot_mp
                )
                processed.op = RegProcessOperation.OP_COPY
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
                    processed.op = RegProcessOperation.OP_DOWNLOAD

                # set file permission as RegInf says
                os.chown(dst, reginf.uid, reginf.gid)
                os.chmod(dst, reginf.mode)

        processed.size = dst.stat().st_size
        processed.elapsed_ns = time.thread_time_ns() - begin_time

        self.stats_collector.report(processed)

    def _create_regular_files(self, regulars_file: str):
        logger.info("start to populate regular files...")
        # get total number of regular_files
        with open(regulars_file, "r") as f:
            total_files_num = len(f.readlines())

        # NOTE: check _OtaStatisticsStorage for available attributes
        logger.info(f"total_regular_files={total_files_num}")
        self.stats_collector.set_total_regular_files(total_files_num)

        self._hardlink_register = HardlinkRegister()

        # limit the cocurrent tasks and downloading
        _download_se = Semaphore(self.MAX_CONCURRENT_DOWNLOAD)
        _tasks_tracker = SimpleTasksTracker(
            max_concurrent=self.MAX_CONCURRENT_TASKS,
            title="process_regulars",
        )

        with open(regulars_file, "r") as f, ThreadPoolExecutor(
            thread_name_prefix="create_standby_slot"
        ) as pool:
            for entry_line in f:
                entry = RegularInf.parse_reginf(entry_line)

                # interrupt update if _tasks_tracker collects error
                if e := _tasks_tracker.last_error:
                    logger.error(f"interrupt update due to {e!r}")
                    raise e

                fut = pool.submit(
                    self._create_regular_file,
                    entry,
                    download_se=_download_se,
                )
                _tasks_tracker.add_task(fut)
                fut.add_done_callback(_tasks_tracker.done_callback)

            logger.info(
                "all process_regulars tasks are dispatched, wait for finishing..."
            )
            _tasks_tracker.task_collect_finished()
            _tasks_tracker.wait(self.stats_collector.wait_staging)

    ###### public API methods ######
    @classmethod
    def should_erase_standby_slot(cls) -> bool:
        return True

    @classmethod
    def is_standby_as_ref(cls) -> bool:
        return False

    def create_standby_slot(self):
        """Exposed API for ota-client."""
        try:
            self._process_directory()
            self._process_symlink()
            self._process_regular()
            self._process_persistent()
        except OTAError:
            raise  # if the error is already specified and wrapped, just raise again
        except DownloadFailedSpaceNotEnough:
            raise StandbySlotSpaceNotEnoughError from None
        except (ExceedMaxRetryError, ChunkStreamingError) as e:
            raise NetworkError from e
        except Exception as e:
            # TODO: define specified error code
            raise ApplyOTAUpdateFailed from e
