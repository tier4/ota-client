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
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from ..errors import ApplyOTAUpdateFailed, OTAError, StandbySlotSpaceNotEnoughError
from ..common import SimpleTasksTracker, OTAFileCacheControl, urljoin_ensure_base
from ..configs import config as cfg
from ..copy_tree import CopyTree
from ..downloader import (
    ChunkStreamingError,
    DownloadFailedSpaceNotEnough,
    Downloader,
    ExceedMaxRetryError,
)
from ..errors import NetworkError
from ..ota_metadata import (
    DirectoryInf,
    PersistentInf,
    RegularInf,
    SymbolicLinkInf,
)
from ..proto import wrapper
from ..proxy_info import proxy_cfg
from ..update_stats import (
    OTAUpdateStatsCollector,
    RegInfProcessedStats,
    RegProcessOperation,
)
from .. import log_util

from .common import HardlinkRegister
from .interface import StandbySlotCreatorProtocol, UpdateMeta

logger = log_util.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


# implementation
class LegacyMode(StandbySlotCreatorProtocol):
    MAX_CONCURRENT_TASKS = cfg.MAX_CONCURRENT_TASKS

    def __init__(
        self,
        *,
        update_meta: UpdateMeta,
        stats_collector: OTAUpdateStatsCollector,
        update_phase_tracker: Callable,
        downloader: Downloader,
    ) -> None:
        self.cookies = update_meta.cookies
        self.metadata = update_meta.metadata
        self.url_base = update_meta.url_base

        self.stats_collector = stats_collector
        self.update_phase_tracker: Callable = update_phase_tracker

        self.reference_slot_mp = Path(update_meta.ref_slot_mount_point)
        self.standby_slot_mp = Path(update_meta.standby_slot_mount_point)
        self.boot_dir = Path(update_meta.boot_dir)

        # internal used vars
        self._passwd_file = Path(cfg.PASSWD_FILE)
        self._group_file = Path(cfg.GROUP_FILE)

        # configure the downloader
        self._downloader = downloader
        self.proxies = None
        if proxy := proxy_cfg.get_proxy_for_local_ota():
            logger.info(f"configuring {proxy=}")
            self.proxies = {"http": proxy}

    def _process_directory(self):
        self.update_phase_tracker(wrapper.StatusProgressPhase.DIRECTORY)

        list_info = self.metadata.directory
        with tempfile.NamedTemporaryFile(prefix=__name__) as f:
            url = urljoin_ensure_base(self.url_base, list_info.file)
            # NOTE: do not use cache when fetching dir list
            self._downloader.download(
                url,
                Path(f.name),
                digest=list_info.hash,
                proxies=self.proxies,
                cookies=self.cookies,
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

        list_info = self.metadata.symboliclink
        with tempfile.NamedTemporaryFile(prefix=__name__) as f:
            url = urljoin_ensure_base(self.url_base, list_info.file)
            # NOTE: do not use cache when fetching symlink list
            self._downloader.download(
                url,
                Path(f.name),
                digest=list_info.hash,
                proxies=self.proxies,
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

        list_info = self.metadata.regular
        with tempfile.NamedTemporaryFile(prefix=__name__) as f:
            url = urljoin_ensure_base(self.url_base, list_info.file)
            # download the regulars.txt
            # NOTE: do not use cache when fetching regular files list
            self._downloader.download(
                url,
                Path(f.name),
                digest=list_info.hash,
                proxies=self.proxies,
                cookies=self.cookies,
                headers={
                    OTAFileCacheControl.header_lower.value: OTAFileCacheControl.no_cache.value
                },
            )

            self._create_regular_files(f.name)

    def _process_persistent(self):
        self.update_phase_tracker(wrapper.StatusProgressPhase.PERSISTENT)

        list_info = self.metadata.persistent
        with tempfile.NamedTemporaryFile(prefix=__name__) as f:
            url = urljoin_ensure_base(self.url_base, list_info.file)
            # NOTE: do not use cache when fetching persist files list
            self._downloader.download(
                url,
                Path(f.name),
                digest=list_info.hash,
                proxies=self.proxies,
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

    def _create_regular_file(self, reginf: RegularInf):
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
                        url, compression_alg = self.metadata.get_download_url(
                            reginf, base_url=self.url_base
                        )
                        processed.errors = self._downloader.download(
                            url,
                            dst,
                            digest=reginf.sha256hash,
                            size=reginf.size,
                            proxies=self.proxies,
                            cookies=self.cookies,
                            compression_alg=compression_alg,
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
                url, compression_alg = self.metadata.get_download_url(
                    reginf, base_url=self.url_base
                )
                processed.errors = self._downloader.download(
                    url,
                    dst,
                    digest=reginf.sha256hash,
                    size=reginf.size,
                    proxies=self.proxies,
                    cookies=self.cookies,
                    compression_alg=compression_alg,
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

                fut = pool.submit(self._create_regular_file, entry)
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
