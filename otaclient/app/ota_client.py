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

from . import downloader, ota_metadata, errors as ota_errors
from .boot_control import BootControllerProtocol, get_boot_controller
from .common import (
    get_backoff,
    ensure_otaproxy_start,
    RetryTaskMap,
    RetryTaskMapInterrupted,
)
from .configs import config as cfg
from .create_standby import StandbySlotCreatorProtocol, get_standby_slot_creator
from .ecu_info import ECUInfo
from .interface import OTAClientProtocol
from .ota_status import LiveOTAStatus
from .proto import wrapper
from .update_stats import (
    OTAUpdateStatsCollector,
    RegInfProcessedStats,
    RegProcessOperation,
)
from . import log_setting

try:
    from otaclient import __version__  # type: ignore
except ImportError:
    __version__ = "unknown"

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

    def is_can_reboot_flag_set(self) -> bool:
        return self._can_reboot.is_set()

    def wait_can_reboot_flag(self):
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
        self._otameta: ota_metadata.OTAMetadata = None  # type: ignore
        self._url_base: str = None  # type: ignore

        # dynamic update status
        self.total_files_size_uncompressed = 0
        self.total_files_num = 0
        self.total_download_files_num = 0
        self.total_download_fiies_size = 0
        self.total_remove_files_num = 0

        # init downloader
        self._downloader = downloader.Downloader()
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

        def _download_file(entry: wrapper.RegularInf) -> RegInfProcessedStats:
            """Download single OTA image file."""
            cur_stat = RegInfProcessedStats(op=RegProcessOperation.DOWNLOAD_REMOTE_COPY)

            _fhash_str = entry.get_hash()
            # special treatment to empty file
            if _fhash_str == downloader.EMPTY_FILE_SHA256:
                return cur_stat

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

        logger.debug("download neede OTA image files...")
        # special treatment to empty file, create it first
        _empty_file = self._ota_tmp_on_standby / downloader.EMPTY_FILE_SHA256
        _empty_file.touch()

        last_active_timestamp = int(time.time())
        _mapper = RetryTaskMap(
            max_concurrent=cfg.MAX_CONCURRENT_DOWNLOAD_TASKS,
            backoff_func=partial(
                get_backoff,
                factor=cfg.DOWNLOAD_GROUP_BACKOFF_FACTOR,
                _max=cfg.DOWNLOAD_GROUP_BACKOFF_MAX,
            ),
            max_retry=0,  # NOTE: we use another strategy below
        )
        for task_result in _mapper.map(_download_file, download_list):
            _fut, _entry = task_result
            if not _fut.exception():
                self._update_stats_collector.report_download_ota_files(_fut.result())
                last_active_timestamp = int(time.time())
                continue

            # on failed task
            # NOTE: for failed task, it must has retried <DOWNLOAD_RETRY>
            #       time, so we manually create one download report
            logger.debug(f"failed to download {_entry=}: {_fut}")
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
                _mapper.shutdown(raise_last_exc=True)

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
            _err_msg = f"failed to generate delta: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.UpdateDeltaGenerationFailed(
                _err_msg, module=__name__
            ) from e

        # --- download needed files --- #
        logger.info(
            "start to download needed files..."
            f"total_download_files_size={_delta_bundle.total_download_files_size:,}bytes"
        )
        self.update_phase = wrapper.UpdatePhase.DOWNLOADING_OTA_FILES
        try:
            self._download_files(_delta_bundle.get_download_list())
        except downloader.DownloadFailedSpaceNotEnough:
            _err_msg = "not enough space is left on standby slot"
            logger.error(_err_msg)
            raise ota_errors.StandbySlotInsufficientSpace(
                _err_msg, module=__name__
            ) from None
        except RetryTaskMapInterrupted as e:
            _err_msg = (
                f"downloading group keeps failing for {cfg.DOWNLOAD_GROUP_INACTIVE_TIMEOUT}s, "
                f"last_error: {e!r}"
            )
            logger.error(_err_msg)
            raise ota_errors.NetworkError(_err_msg, module=__name__) from e
        except Exception as e:
            _err_msg = f"unspecific error, failed to finish downloading files: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.NetworkError(_err_msg, module=__name__) from e

        # shutdown downloader on download finished
        self._downloader.shutdown()

        # ------ in_update ------ #
        logger.info("start to apply changes to standby slot...")
        self.update_phase = wrapper.UpdatePhase.APPLYING_UPDATE
        try:
            self._standby_slot_creator.create_standby_slot()
        except Exception as e:
            _err_msg = f"failed to apply update to standby slot: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.ApplyOTAUpdateFailed(_err_msg, module=__name__) from e

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
            _err_msg = f"cookie is invalid: {cookies_json=}"
            logger.error(_err_msg)
            raise ota_errors.InvalidUpdateRequest(_err_msg, module=__name__) from e

        # configure proxy
        logger.debug("configure proxy setting...")
        if self.proxy:
            logger.info(
                f"use {self.proxy=} for local OTA update, "
                f"wait for otaproxy@{self.proxy} online..."
            )
            ensure_otaproxy_start(self.proxy)
            # NOTE(20221013): check requests document for how to set proxy,
            #                 we only support using http proxy here.
            self._downloader.configure_proxies({"http": self.proxy})

        # process metadata.jwt and ota metafiles
        logger.debug("process metadata.jwt...")
        try:
            self._otameta = ota_metadata.OTAMetadata(
                url_base=self._url_base,
                downloader=self._downloader,
            )
            self.total_files_num = self._otameta.total_files_num
            self.total_files_size_uncompressed = (
                self._otameta.total_files_size_uncompressed
            )
        except downloader.HashVerificaitonError as e:
            _err_msg = f"downloader: keep failing to verify ota metafiles' hash: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.MetadataJWTVerficationFailed(
                _err_msg, module=__name__
            ) from e
        except downloader.DestinationNotAvailableError as e:
            _err_msg = f"downloader: failed to save ota metafiles: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.OTAErrorUnRecoverable(_err_msg, module=__name__) from e
        except ota_metadata.MetadataJWTVerificationFailed as e:
            _err_msg = f"failed to verify metadata.jwt: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.MetadataJWTVerficationFailed(
                _err_msg, module=__name__
            ) from e
        except ota_metadata.MetadataJWTPayloadInvalid as e:
            _err_msg = f"metadata.jwt is invalid: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.MetadataJWTInvalid(_err_msg, module=__name__) from e
        except Exception as e:
            _err_msg = f"unspecific error, failed to prepare ota metafiles: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.OTAMetaDownloadFailed(_err_msg, module=__name__) from e

        # ------ execute local update ------ #
        logger.info("enter local OTA update...")
        try:
            self._update_standby_slot()
        except ota_errors.OTAError:
            raise  # no need to wrap an OTAError again
        except Exception as e:
            raise ota_errors.ApplyOTAUpdateFailed(
                f"unspecific applying OTA update failure: {e!r}", module=__name__
            )

        # ------ post update ------ #
        logger.info("local update finished, wait on all subecs...")
        logger.info("enter boot control post update phase...")
        # boot controller postupdate
        self.update_phase = wrapper.UpdatePhase.PROCESSING_POSTUPDATE
        next(_postupdate_gen := self._boot_controller.post_update())

        # wait for sub ecu if needed before rebooting
        self._control_flags.wait_can_reboot_flag()
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
        except ota_errors.OTAError as e:
            logger.error(f"update failed: {e!r}")
            self._boot_controller.on_operation_failure()
            raise  # do not cover the OTA error again
        except Exception as e:
            _err_msg = f"unspecific error, update failed: {e!r}"
            self._boot_controller.on_operation_failure()
            raise ota_errors.ApplyOTAUpdateFailed(_err_msg, module=__name__) from e
        finally:
            self.shutdown()


class _OTARollbacker:
    def __init__(self, boot_controller: BootControllerProtocol) -> None:
        self._boot_controller = boot_controller

    def execute(self):
        try:
            self._boot_controller.pre_rollback()
            self._boot_controller.post_rollback()
        except ota_errors.OTAError as e:
            logger.error(f"rollback failed: {e!r}")
            self._boot_controller.on_operation_failure()
            raise


class OTAClient(OTAClientProtocol):
    """
    Init params:
        boot_controller: boot control instance
        create_standby_cls: type of create standby slot mechanism to use
        my_ecu_id: ECU id of the device running this otaclient instance
        control_flags: flags used by otaclient and ota_service stub for synchronization
        proxy: upper otaproxy URL
    """

    OTACLIENT_VERSION = __version__

    def __init__(
        self,
        *,
        boot_controller: BootControllerProtocol,
        create_standby_cls: Type[StandbySlotCreatorProtocol],
        my_ecu_id: str,
        control_flags: OTAClientControlFlags,
        proxy: Optional[str] = None,
    ):
        try:
            self.my_ecu_id = my_ecu_id
            # ensure only one update/rollback session is running
            self._lock = threading.Lock()

            self.boot_controller = boot_controller
            self.create_standby_cls = create_standby_cls
            self.live_ota_status = LiveOTAStatus(
                self.boot_controller.get_booted_ota_status()
            )

            self.current_version = self.boot_controller.load_version()
            self.proxy = proxy
            self.control_flags = control_flags

            # executors for update/rollback
            self._update_executor: _OTAUpdater = None  # type: ignore
            self._rollback_executor: _OTARollbacker = None  # type: ignore

            # err record
            self.last_failure_type = wrapper.FailureType.NO_FAILURE
            self.last_failure_reason = ""
            self.last_failure_traceback = ""
        except Exception as e:
            _err_msg = f"failed to start otaclient core: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.OTAClientStartupFailed(_err_msg, module=__name__) from e

    def _on_failure(self, exc: ota_errors.OTAError, ota_status: wrapper.StatusOta):
        self.live_ota_status.set_ota_status(ota_status)
        try:
            self.last_failure_type = exc.failure_type
            self.last_failure_reason = exc.get_failure_reason()
            if cfg.DEBUG_MODE:
                self.last_failure_traceback = exc.get_failure_traceback()

            logger.error(
                exc.get_error_report(f"OTA failed with {ota_status.name}: {exc!r}")
            )
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

                # reset failure information on handling new update request
                self.last_failure_type = wrapper.FailureType.NO_FAILURE
                self.last_failure_reason = ""
                self.last_failure_traceback = ""

                # enter update
                self.live_ota_status.set_ota_status(wrapper.StatusOta.UPDATING)
                self._update_executor.execute(version, url_base, cookies_json)
            except ota_errors.OTAError as e:
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

                # clear failure information on handling new rollback request
                self.last_failure_type = wrapper.FailureType.NO_FAILURE
                self.last_failure_reason = ""
                self.last_failure_traceback = ""

                # entering rollback
                self.live_ota_status.set_ota_status(wrapper.StatusOta.ROLLBACKING)
                self._rollback_executor.execute()
            # silently ignore overlapping request
            except ota_errors.OTAError as e:
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
            otaclient_version=self.OTACLIENT_VERSION,
            ota_status=live_ota_status,
            failure_type=self.last_failure_type,
            failure_reason=self.last_failure_reason,
            failure_traceback=self.last_failure_traceback,
        )
        if live_ota_status == wrapper.StatusOta.UPDATING and self._update_executor:
            status_report.update_status = self._update_executor.get_update_status()
        return status_report


class OTAServicer:
    def __init__(
        self,
        *,
        control_flags: OTAClientControlFlags,
        ecu_info: ECUInfo,
        executor: Optional[ThreadPoolExecutor] = None,
        otaclient_version: str = __version__,
        proxy: Optional[str] = None,
    ) -> None:
        self.ecu_id = ecu_info.ecu_id
        self.otaclient_version = otaclient_version
        self.local_used_proxy_url = proxy
        self.last_operation: Optional[wrapper.StatusOta] = None

        # default boot startup failure if boot_controller/otaclient_core crashed without
        # raising specific error
        self._otaclient_startup_failed_status = wrapper.StatusResponseEcuV2(
            ecu_id=ecu_info.ecu_id,
            otaclient_version=otaclient_version,
            ota_status=wrapper.StatusOta.FAILURE,
            failure_type=wrapper.FailureType.UNRECOVERABLE,
            failure_reason="unspecific error",
        )
        self._update_rollback_lock = asyncio.Lock()
        self._run_in_executor = partial(
            asyncio.get_running_loop().run_in_executor, executor
        )

        #
        # ------ compose otaclient ------
        #
        self._otaclient_inst: Optional[OTAClient] = None

        # select boot_controller and standby_slot implementations
        _bootctrl_cls = get_boot_controller(ecu_info.get_bootloader())
        _standby_slot_creator = get_standby_slot_creator(cfg.STANDBY_CREATION_MODE)

        # boot controller starts up
        try:
            _bootctrl_inst = _bootctrl_cls()
        except ota_errors.OTAError as e:
            logger.error(
                e.get_error_report(title=f"boot controller startup failed: {e!r}")
            )
            self._otaclient_startup_failed_status = wrapper.StatusResponseEcuV2(
                ecu_id=ecu_info.ecu_id,
                otaclient_version=otaclient_version,
                ota_status=wrapper.StatusOta.FAILURE,
                failure_type=wrapper.FailureType.UNRECOVERABLE,
                failure_reason=e.get_failure_reason(),
            )

            if cfg.DEBUG_MODE:
                self._otaclient_startup_failed_status.failure_traceback = (
                    e.get_failure_traceback()
                )
            return

        # otaclient core starts up
        try:
            self._otaclient_inst = OTAClient(
                boot_controller=_bootctrl_inst,
                create_standby_cls=_standby_slot_creator,
                my_ecu_id=ecu_info.ecu_id,
                control_flags=control_flags,
                proxy=proxy,
            )
        except ota_errors.OTAError as e:
            logger.error(
                e.get_error_report(title=f"otaclient core startup failed: {e!r}")
            )
            self._otaclient_startup_failed_status = wrapper.StatusResponseEcuV2(
                ecu_id=ecu_info.ecu_id,
                otaclient_version=otaclient_version,
                ota_status=wrapper.StatusOta.FAILURE,
                failure_type=wrapper.FailureType.UNRECOVERABLE,
                failure_reason=e.get_failure_reason(),
            )

            if cfg.DEBUG_MODE:
                self._otaclient_startup_failed_status.failure_traceback = (
                    e.get_failure_traceback()
                )
            return

    @property
    def is_busy(self) -> bool:
        return self._update_rollback_lock.locked()

    async def dispatch_update(
        self, request: wrapper.UpdateRequestEcu
    ) -> wrapper.UpdateResponseEcu:
        # prevent update operation if otaclient is not started
        if self._otaclient_inst is None:
            return wrapper.UpdateResponseEcu(
                ecu_id=self.ecu_id, result=wrapper.FailureType.UNRECOVERABLE
            )

        # check and acquire lock
        if self._update_rollback_lock.locked():
            logger.warning(
                f"ongoing operation: {self.last_operation=}, ignore incoming {request=}"
            )
            return wrapper.UpdateResponseEcu(
                ecu_id=self.ecu_id, result=wrapper.FailureType.RECOVERABLE
            )

        # immediately take the lock if not locked
        await self._update_rollback_lock.acquire()
        self.last_operation = wrapper.StatusOta.UPDATING

        async def _update_task():
            if self._otaclient_inst is None:
                return

            try:
                await self._run_in_executor(
                    partial(
                        self._otaclient_inst.update,
                        request.version,
                        request.url,
                        request.cookies,
                    )
                )
            except Exception:
                pass  # error should be collected by otaclient, not us
            finally:
                self.last_operation = None
                self._update_rollback_lock.release()

        # dispatch update to background
        asyncio.create_task(_update_task())

        return wrapper.UpdateResponseEcu(
            ecu_id=self.ecu_id, result=wrapper.FailureType.NO_FAILURE
        )

    async def dispatch_rollback(
        self, request: wrapper.RollbackRequestEcu
    ) -> wrapper.RollbackResponseEcu:
        # prevent rollback operation if otaclient is not started
        if self._otaclient_inst is None:
            return wrapper.RollbackResponseEcu(
                ecu_id=self.ecu_id, result=wrapper.FailureType.UNRECOVERABLE
            )

        # check and acquire lock
        if self._update_rollback_lock.locked():
            logger.warning(
                f"ongoing operation: {self.last_operation=}, ignore incoming {request=}"
            )
            return wrapper.RollbackResponseEcu(
                ecu_id=self.ecu_id, result=wrapper.FailureType.RECOVERABLE
            )

        # immediately take the lock if not locked
        await self._update_rollback_lock.acquire()
        self.last_operation = wrapper.StatusOta.ROLLBACKING

        async def _rollback_task():
            if self._otaclient_inst is None:
                return

            try:
                await self._run_in_executor(self._otaclient_inst.rollback)
            except Exception:
                pass  # error should be collected by otaclient, not us
            finally:
                self.last_operation = None
                self._update_rollback_lock.release()

        # dispatch to background
        asyncio.create_task(_rollback_task())

        return wrapper.RollbackResponseEcu(
            ecu_id=self.ecu_id, result=wrapper.FailureType.NO_FAILURE
        )

    async def get_status(self) -> wrapper.StatusResponseEcuV2:
        # otaclient is not started due to boot control startup failed
        if self._otaclient_inst is None:
            return self._otaclient_startup_failed_status

        # otaclient core started, query status from it
        return await self._run_in_executor(self._otaclient_inst.status)
