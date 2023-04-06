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


import asyncio
import gc
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from json.decoder import JSONDecodeError
from pathlib import Path
from typing import Optional, Type, Iterator
from urllib.parse import urlparse

from otaclient import __version__  # type: ignore
from .ecu_info import ECUInfo
from .errors import (
    BaseOTAMetaVerificationFailed,
    NetworkError,
    OTA_APIError,
    OTAError,
    OTARollbackError,
    OTAUpdateError,
)
from .boot_control import BootControllerProtocol, get_boot_controller
from .common import (
    RetryTaskMap,
    ensure_http_server_open,
    wait_with_backoff,
)
from .configs import config as cfg
from .create_standby import StandbySlotCreatorProtocol, get_standby_slot_creator
from .downloader import (
    DestinationNotAvailableError,
    DownloadFailedSpaceNotEnough,
    Downloader,
    HashVerificaitonError,
)
from .interface import OTAClientProtocol
from .errors import (
    ApplyOTAUpdateFailed,
    InvalidUpdateRequest,
    OTAMetaVerificationFailed,
    OTAErrorUnRecoverable,
    OTAMetaDownloadFailed,
    StandbySlotSpaceNotEnoughError,
    UpdateDeltaGenerationFailed,
)
from .ota_metadata import OTAMetadata
from .ota_status import LiveOTAStatus
from .proto import wrapper
from .update_stats import (
    OTAUpdateStatsCollector,
    RegInfProcessedStats,
    RegProcessOperation,
)
from . import log_setting

logger = log_setting.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class OTAClientControlFlags:
    """
    When self ECU's otaproxy is enabled, all the child ECUs of this ECU
        and self ECU OTA update will depend on its otaproxy, we need to
        control when otaclient can start its downloading/reboot with considering
        whether local otaproxy is started/required.
    """

    def __init__(self) -> None:
        self._can_reboot = threading.Event()

    def otaclient_wait_for_reboot(self):
        self._can_reboot.wait()

    def set_can_reboot_flag(self):
        self._can_reboot.set()

    def clear_can_reboot_flag(self):
        self._can_reboot.clear()


class _OTAUpdater:
    """The implementation of OTA update logic."""

    def __init__(
        self,
        *,
        boot_controller: BootControllerProtocol,
        create_standby_cls: Type[StandbySlotCreatorProtocol],
        proxy: Optional[str] = None,
        control_flags: OTAClientControlFlags,
    ) -> None:
        self._control_flags = control_flags
        self._boot_controller = boot_controller
        self._create_standby_cls = create_standby_cls

        # init update status
        self.update_phase = wrapper.UpdatePhase.INITIALIZING
        self.update_start_time = 0
        self.updating_version: str = ""
        self.failure_reason = ""
        # init variables needed for update
        self._otameta: OTAMetadata = None  # type: ignore
        self._url_base: str = None  # type: ignore

        # dynamic update status
        self.total_files_size_uncompressed = 0
        self.total_files_num = 0
        self.total_download_files_num = 0
        self.total_download_fiies_size = 0
        self.total_remove_files_num = 0

        # init downloader
        self._downloader = Downloader()
        self.proxy = proxy

        # init ota update statics collector
        self._update_stats_collector = OTAUpdateStatsCollector()

        # paths
        self._ota_tmp_on_standby = Path(cfg.MOUNT_POINT) / Path(
            cfg.OTA_TMP_STORE
        ).relative_to("/")
        self._ota_tmp_image_meta_dir_on_standby = Path(cfg.MOUNT_POINT) / Path(
            cfg.OTA_TMP_META_STORE
        ).relative_to("/")

    # helper methods

    def _download_files(self, download_list: Iterator[wrapper.RegularInf]):
        """Download all needed OTA image files indicated by calculated bundle."""
        logger.debug("download neede OTA image files...")

        def _download_file(entry: wrapper.RegularInf) -> RegInfProcessedStats:
            """Download single OTA image file."""
            cur_stat = RegInfProcessedStats(op=RegProcessOperation.DOWNLOAD_REMOTE_COPY)

            _fhash_str = entry.get_hash()
            _local_copy = self._ota_tmp_on_standby / _fhash_str
            entry_url, compression_alg = self._otameta.get_download_url(entry)
            cur_stat.download_errors, _, _ = self._downloader.download(
                entry_url,
                _local_copy,
                digest=_fhash_str,
                size=entry.size,
                compression_alg=compression_alg,
            )
            cur_stat.size = _local_copy.stat().st_size
            return cur_stat

        last_active_timestamp = int(time.time())
        with ThreadPoolExecutor(thread_name_prefix="downloading") as _executor:
            _mapper = RetryTaskMap(
                title="downloading_ota_files",
                max_concurrent=cfg.MAX_CONCURRENT_DOWNLOAD_TASKS,
                retry_interval_f=partial(
                    wait_with_backoff,
                    _backoff_factor=cfg.DOWNLOAD_GROUP_BACKOFF_FACTOR,
                    _backoff_max=cfg.DOWNLOAD_GROUP_BACKOFF_MAX,
                ),
                max_retry=0,  # NOTE: we use another strategy below
                executor=_executor,
            )
            for _, task_result in _mapper.map(_download_file, download_list):
                is_successful, entry, fut = task_result
                if is_successful:
                    self._update_stats_collector.report_download_ota_files(fut.result())
                    last_active_timestamp = int(time.time())
                    continue

                # on failed task
                # NOTE: for failed task, it must has retried <DOWNLOAD_RETRY>
                #       time, so we manually create one download report
                logger.debug(f"failed to download {entry=}: {fut}")
                self._update_stats_collector.report_download_ota_files(
                    RegInfProcessedStats(
                        op=RegProcessOperation.DOWNLOAD_ERROR_REPORT,
                        download_errors=cfg.DOWNLOAD_RETRY,
                    ),
                )
                # if the download group becomes inactive longer than <limit>,
                # force shutdown and breakout.
                # NOTE: considering the edge condition that all downloading threads
                #       are downloading large file, resulting time cost longer than
                #       timeout limit, and one task is interrupted and yielded,
                #       we should not breakout on this situation as other threads are
                #       still downloading.
                last_active_timestamp = max(
                    last_active_timestamp, self._downloader.last_active_timestamp
                )
                if (
                    int(time.time()) - last_active_timestamp
                    > cfg.DOWNLOAD_GROUP_INACTIVE_TIMEOUT
                ):
                    logger.error(
                        f"downloader becomes stuck for {cfg.DOWNLOAD_GROUP_INACTIVE_TIMEOUT=} seconds, abort"
                    )
                    _mapper.shutdown()

        # all tasks are finished, waif for stats collector to finish processing
        # all the reported stats
        self._update_stats_collector.wait_staging()

    def _update_standby_slot(self):
        """Apply OTA update to standby slot."""
        # ------ pre_update ------ #
        # --- prepare standby slot --- #
        # NOTE: erase standby slot or not based on the used StandbySlotCreator
        logger.debug("boot controller prepares standby slot...")
        self._boot_controller.pre_update(
            self.updating_version,
            standby_as_ref=False,  # NOTE: this option is deprecated and not used by bootcontroller
            erase_standby=self._create_standby_cls.should_erase_standby_slot(),
        )
        # prepare the tmp storage on standby slot after boot_controller.pre_update finished
        self._ota_tmp_on_standby.mkdir(exist_ok=True)
        self._ota_tmp_image_meta_dir_on_standby.mkdir(exist_ok=True)

        # --- init standby_slot creator, calculate delta --- #
        logger.info("start to calculate and prepare delta...")
        self.update_phase = wrapper.UpdatePhase.CALCULATING_DELTA
        self._standby_slot_creator = self._create_standby_cls(
            ota_metadata=self._otameta,
            boot_dir=str(self._boot_controller.get_standby_boot_dir()),
            standby_slot_mount_point=cfg.MOUNT_POINT,
            active_slot_mount_point=cfg.ACTIVE_ROOT_MOUNT_POINT,
            stats_collector=self._update_stats_collector,
        )
        try:
            _delta_bundle = self._standby_slot_creator.calculate_and_prepare_delta()
            # update dynamic information
            self.total_download_files_num = len(_delta_bundle.download_list)
            self.total_download_fiies_size = _delta_bundle.total_download_files_size
            self.total_remove_files_num = len(_delta_bundle.rm_delta)
        except Exception as e:
            logger.error(f"failed to generate delta: {e!r}")
            raise UpdateDeltaGenerationFailed from e

        # --- download needed files --- #
        logger.info(
            "start to download needed files..."
            f"total_download_files_size={_delta_bundle.total_download_files_size:,}bytes"
        )
        self.update_phase = wrapper.UpdatePhase.DOWNLOADING_OTA_FILES
        try:
            self._download_files(_delta_bundle.get_download_list())
        except DownloadFailedSpaceNotEnough:
            logger.critical("not enough space is left on standby slot")
            raise StandbySlotSpaceNotEnoughError from None
        except Exception as e:
            logger.error(f"failed to finish downloading files: {e!r}")
            raise NetworkError from e

        # shutdown downloader on download finished
        self._downloader.shutdown()

        # ------ in_update ------ #
        logger.info("start to apply changes to standby slot...")
        self.update_phase = wrapper.UpdatePhase.APPLYING_UPDATE
        try:
            self._standby_slot_creator.create_standby_slot()
        except Exception as e:
            logger.error(f"failed to apply update to standby slot: {e!r}")
            raise ApplyOTAUpdateFailed from e
        logger.info("finished updating standby slot")

    def _execute_update(
        self,
        version: str,
        raw_url_base: str,
        cookies_json: str,
    ):
        """OTA update workflow implementation.

        e.g.
        cookies = {
            "CloudFront-Policy": "eyJTdGF0ZW1lbnQ...",
            "CloudFront-Signature": "o4ojzMrJwtSIg~izsy...",
            "CloudFront-Key-Pair-Id": "K2...",
        }
        """
        logger.info(f"execute local update: {version=},{raw_url_base=}")
        logger.debug(f"{cookies_json=}")

        self.updating_version = version
        self.update_start_time = time.time_ns()
        self.failure_reason = ""  # clean failure reason

        self._update_stats_collector.start()

        # ------ init, processing metadata ------ #
        self.update_phase = wrapper.UpdatePhase.PROCESSING_METADATA
        # parse url_base
        # unconditionally regulate the url_base
        _url_base = urlparse(raw_url_base)
        _path = f"{_url_base.path.rstrip('/')}/"
        self._url_base = _url_base._replace(path=_path).geturl()

        # parse cookies
        logger.debug("process cookies_json...")
        try:
            _cookies = json.loads(cookies_json)
            assert isinstance(
                _cookies, dict
            ), f"invalid cookies, expecting json object: {cookies_json}"
            self._downloader.configure_cookies(_cookies)
        except (JSONDecodeError, AssertionError) as e:
            raise InvalidUpdateRequest from e

        # configure proxy
        logger.debug("configure proxy setting...")
        if self.proxy:
            logger.info(f"use {self.proxy=} for local OTA update")
            logger.debug("wait for otaproxy to become ready...")
            # TODO: make otaproxy scrubing not blocking the otaproxy starts
            ensure_http_server_open(self.proxy)
            # NOTE(20221013): check requests document for how to set proxy,
            #                 we only support using http proxy here.
            self._downloader.configure_proxies({"http": self.proxy})

        # process metadata.jwt and ota metafiles
        logger.debug("process metadata.jwt...")
        try:
            self._otameta = OTAMetadata(
                url_base=self._url_base,
                downloader=self._downloader,
            )
            self.total_files_num = self._otameta.total_files_num
            self.total_files_size_uncompressed = (
                self._otameta.total_files_size_uncompressed
            )
        except HashVerificaitonError as e:
            logger.error("failed to verify ota metafiles hash")
            raise OTAMetaVerificationFailed from e
        except DestinationNotAvailableError as e:
            logger.error("failed to save ota metafiles")
            raise OTAErrorUnRecoverable from e
        except ValueError as e:
            logger.error(f"failed to verify metadata.jwt: {e!r}")
            raise BaseOTAMetaVerificationFailed from e
        except Exception as e:
            logger.error(f"failed to download ota metafiles: {e!r}")
            raise OTAMetaDownloadFailed from e

        # ------ execute local update ------ #
        logger.info("enter local OTA update...")
        try:
            self._update_standby_slot()
        except OTAError:
            raise
        except Exception as e:
            raise ApplyOTAUpdateFailed(f"unspecific applying OTA update failure: {e!r}")

        # ------ post update ------ #
        logger.info("local update finished, wait on all subecs...")
        logger.info("enter boot control post update phase...")
        # boot controller postupdate
        self.update_phase = wrapper.UpdatePhase.PROCESSING_POSTUPDATE
        next(_postupdate_gen := self._boot_controller.post_update())

        # wait for sub ecu if needed before rebooting
        self._control_flags.otaclient_wait_for_reboot()
        next(_postupdate_gen, None)  # reboot

    # API

    def shutdown(self):
        self.update_phase = wrapper.UpdatePhase.INITIALIZING
        self._downloader.shutdown()
        self._update_stats_collector.stop()

    def get_update_status(self) -> wrapper.UpdateStatus:
        """
        Returns:
            A tuple contains the version and the update_progress.
        """
        update_progress = self._update_stats_collector.get_snapshot()
        # update static information
        # NOTE: timestamp is in seconds
        update_progress.update_start_timestamp = self.update_start_time // 1_000_000_000
        update_progress.update_firmware_version = self.updating_version
        # update dynamic information
        update_progress.total_files_size_uncompressed = (
            self.total_files_size_uncompressed
        )
        update_progress.total_files_num = self.total_files_num
        update_progress.total_download_files_num = self.total_download_files_num
        update_progress.total_download_files_size = self.total_download_fiies_size
        update_progress.total_remove_files_num = self.total_remove_files_num
        # downloading stats
        update_progress.downloaded_bytes = self._downloader.downloaded_bytes
        update_progress.downloading_elapsed_time = wrapper.Duration(
            seconds=self._downloader.downloader_active_seconds
        )

        # update other information
        update_progress.phase = self.update_phase
        update_progress.total_elapsed_time = wrapper.Duration.from_nanoseconds(
            time.time_ns() - self.update_start_time
        )
        return update_progress

    def execute(
        self,
        version: str,
        raw_url_base: str,
        cookies_json: str,
    ):
        """Main entry for executing local OTA update.

        Handles OTA failure and logging/finalizing on failure.
        """
        try:
            self._execute_update(version, raw_url_base, cookies_json)
        except OTAError as e:
            logger.error(f"update failed: {e!r}")
            self._boot_controller.on_operation_failure()
            raise OTAUpdateError(e) from e
        except Exception as e:
            raise OTAUpdateError(
                ApplyOTAUpdateFailed(f"unspecific OTA failure: {e!r}")
            ) from e
        finally:
            self.shutdown()


class _OTARollbacker:
    def __init__(self, boot_controller: BootControllerProtocol) -> None:
        self._boot_controller = boot_controller

    def execute(self):
        try:
            # enter rollback
            self._boot_controller.pre_rollback()
            # leave rollback
            self._boot_controller.post_rollback()
        except OTAError as e:
            logger.error(f"rollback failed: {e!r}")
            self._boot_controller.on_operation_failure()
            raise OTARollbackError(e) from e


class OTAClient(OTAClientProtocol):
    """
    Init params:
        boot_control_cls: type of boot control mechanism to use
        create_standby_cls: type of create standby slot mechanism to use
    """

    DEFAULT_FIRMWARE_VERSION = "unknown"

    def __init__(
        self,
        *,
        boot_control_cls: Type[BootControllerProtocol],
        create_standby_cls: Type[StandbySlotCreatorProtocol],
        my_ecu_id: str = "",
        control_flags: OTAClientControlFlags,
        proxy: Optional[str] = None,
    ):
        self.my_ecu_id = my_ecu_id
        # ensure only one update/rollback session is running
        self._lock = threading.Lock()

        self.boot_controller = boot_control_cls()
        self.create_standby_cls = create_standby_cls
        self.live_ota_status = LiveOTAStatus(self.boot_controller.get_ota_status())

        self.current_version = (
            self.boot_controller.load_version() or self.DEFAULT_FIRMWARE_VERSION
        )
        self.proxy = proxy
        self.control_flags = control_flags

        # executors for update/rollback
        self._update_executor: _OTAUpdater = None  # type: ignore
        self._rollback_executor: _OTARollbacker = None  # type: ignore

        # err record
        self.last_failure_type = wrapper.FailureType.NO_FAILURE
        self.last_failure_reason = ""
        self.last_failure_traceback = ""

    def _on_failure(self, exc: OTA_APIError, ota_status: wrapper.StatusOta):
        self.live_ota_status.set_ota_status(ota_status)
        try:
            self.last_failure_type = exc.get_err_type()
            self.last_failure_reason = exc.get_err_reason()
            self.last_failure_traceback = exc.get_traceback()
            logger.error(f"on {ota_status=}: {self.last_failure_traceback=}")
        finally:
            exc = None  # type: ignore , prevent ref cycle

    # API

    def update(self, version: str, url_base: str, cookies_json: str):
        if self._lock.acquire(blocking=False):
            try:
                logger.info("[update] entering...")
                self._update_executor = _OTAUpdater(
                    boot_controller=self.boot_controller,
                    create_standby_cls=self.create_standby_cls,
                    control_flags=self.control_flags,
                    proxy=self.proxy,
                )
                self.last_failure_type = wrapper.FailureType.NO_FAILURE
                self.last_failure_reason = ""
                self.last_failure_traceback = ""
                self.live_ota_status.set_ota_status(wrapper.StatusOta.UPDATING)
                self._update_executor.execute(version, url_base, cookies_json)
            except OTAUpdateError as e:
                self._on_failure(e, wrapper.StatusOta.FAILURE)
            finally:
                self._update_executor = None  # type: ignore
                gc.collect()  # trigger a forced gc
                self._lock.release()
        else:
            logger.warning(
                "ignore incoming rollback request as local update/rollback is ongoing"
            )

    def rollback(self):
        if self._lock.acquire(blocking=False):
            try:
                logger.info("[rollback] entering...")
                self._rollback_executor = _OTARollbacker(
                    boot_controller=self.boot_controller
                )
                self.last_failure_type = wrapper.FailureType.NO_FAILURE
                self.last_failure_reason = ""
                self.last_failure_traceback = ""
                self.live_ota_status.set_ota_status(wrapper.StatusOta.ROLLBACKING)
                self._rollback_executor.execute()
            # silently ignore overlapping request
            except OTARollbackError as e:
                self._on_failure(e, wrapper.StatusOta.ROLLBACK_FAILURE)
            finally:
                self._rollback_executor = None  # type: ignore
                self._lock.release()
        else:
            logger.warning(
                "ignore incoming rollback request as local update/rollback is ongoing"
            )

    def status(self) -> wrapper.StatusResponseEcuV2:
        live_ota_status = self.live_ota_status.get_ota_status()
        status_report = wrapper.StatusResponseEcuV2(
            ecu_id=self.my_ecu_id,
            firmware_version=self.current_version,
            otaclient_version=__version__,
            ota_status=live_ota_status,
            failure_type=self.last_failure_type,
            failure_reason=self.last_failure_reason,
            failure_traceback=self.last_failure_traceback,
        )
        if live_ota_status == wrapper.StatusOta.UPDATING and self._update_executor:
            status_report.update_status = self._update_executor.get_update_status()
        return status_report


class OTAClientBusy(Exception):
    """Raised when otaclient receive another request when doing update/rollback."""


class OTAClientStub:
    def __init__(
        self,
        *,
        ecu_info: ECUInfo,
        executor: ThreadPoolExecutor,
        control_flags: OTAClientControlFlags,
        proxy: Optional[str] = None,
    ) -> None:
        # only one update/rollback is allowed at a time
        self.update_rollback_lock = asyncio.Lock()
        self.my_ecu_id = ecu_info.ecu_id
        self._run_in_executor = partial(
            asyncio.get_running_loop().run_in_executor, executor
        )

        # create otaclient instance and control flags
        self._otaclient = OTAClient(
            boot_control_cls=get_boot_controller(ecu_info.get_bootloader()),
            create_standby_cls=get_standby_slot_creator(cfg.STANDBY_CREATION_MODE),
            my_ecu_id=self.my_ecu_id,
            control_flags=control_flags,
            proxy=proxy,
        )

        # proxy used by local otaclient
        # NOTE: it can be an upper otaproxy, local otaproxy, or no proxy
        self.local_used_proxy_url = proxy
        self.last_operation = None  # update/rollback/None

    # property

    @property
    def is_busy(self) -> bool:
        return self.update_rollback_lock.locked()

    # API method

    async def dispatch_update(self, request: wrapper.UpdateRequestEcu):
        """Dispatch update request to otaclient.

        Raises:
            OTAClientBusy if otaclient is already executing update/rollback.
        """
        if self.update_rollback_lock.locked():
            raise OTAClientBusy(f"ongoing operation: {self.last_operation=}")

        async def _update():
            async with self.update_rollback_lock:
                self.last_operation = wrapper.StatusOta.UPDATING
                try:
                    await self._run_in_executor(
                        partial(
                            self._otaclient.update,
                            request.version,
                            request.url,
                            request.cookies,
                        )
                    )
                finally:
                    self.last_operation = None

        # dispatch update to background
        asyncio.create_task(_update())

    async def dispatch_rollback(self, _: wrapper.RollbackRequestEcu):
        """Dispatch update request to otaclient.

        Raises:
            OTAClientBusy if otaclient is already executing update/rollback.
        """
        if self.update_rollback_lock.locked():
            raise OTAClientBusy(f"ongoing operation: {self.last_operation=}")

        async def _rollback():
            async with self.update_rollback_lock:
                self.last_operation = wrapper.StatusOta.ROLLBACKING
                try:
                    await self._run_in_executor(self._otaclient.rollback)
                finally:
                    self.last_operation = None

        # dispatch to background
        asyncio.create_task(_rollback())

    def get_status(self) -> wrapper.StatusResponseEcuV2:
        return self._otaclient.status()
