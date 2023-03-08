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


import gc
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from json.decoder import JSONDecodeError
from pathlib import Path
from typing import Optional, Type, Iterator
from urllib.parse import urlparse

from otaclient import __version__  # type: ignore
from .errors import (
    BaseOTAMetaVerificationFailed,
    NetworkError,
    OTA_APIError,
    OTAError,
    OTAProxyFailedToStart,
    OTARollbackError,
    OTAUpdateError,
)
from .boot_control import BootControllerProtocol
from .common import RetryTaskMap
from .configs import config as cfg
from .create_standby import StandbySlotCreatorProtocol
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
from .proto.wrapper import UpdatePhase, RegularInf, StatusResponseEcuV2, UpdateStatus
from .proxy_info import proxy_cfg
from .update_stats import (
    OTAUpdateStatsCollector,
    RegInfProcessedStats,
    RegProcessOperation,
)
from . import log_setting

logger = log_setting.get_logger(
    __name__, cfg.LOG_LEVEL_TABLE.get(__name__, cfg.DEFAULT_LOG_LEVEL)
)


class OTAUpdateFSM:
    WAIT_INTERVAL = 3

    def __init__(self) -> None:
        self._ota_proxy_ready = threading.Event()
        self._otaclient_finish_update = threading.Event()
        self._stub_cleanup_finish = threading.Event()
        self.otaclient_failed = threading.Event()
        self.otaservice_failed = threading.Event()

    def on_otaclient_failed(self):
        self.otaclient_failed.set()

    def on_otaservice_failed(self):
        self.otaservice_failed.set()

    def stub_pre_update_ready(self):
        self._ota_proxy_ready.set()

    def client_finish_update(self):
        self._otaclient_finish_update.set()

    def stub_cleanup_finished(self):
        self._stub_cleanup_finish.set()

    def client_wait_for_ota_proxy(self) -> bool:
        """Local otaclient wait for stub(to setup ota_proxy server).

        Return:
            A bool to indicate whether the ota_proxy launching is successful or not.
        """
        while (
            not self.otaservice_failed.is_set() and not self._ota_proxy_ready.is_set()
        ):
            time.sleep(self.WAIT_INTERVAL)
        return not self.otaclient_failed.is_set()

    def client_wait_for_reboot(self) -> bool:
        """Local otaclient should reboot after the stub cleanup finished.

        Return:
            A bool indicates whether the ota_client_stub cleans up successfully.
        """
        while (
            not self.otaservice_failed.is_set()
            and not self._stub_cleanup_finish.is_set()
        ):
            time.sleep(self.WAIT_INTERVAL)
        return not self.otaclient_failed.is_set()

    def stub_wait_for_local_update(self) -> bool:
        """OTA client stub should wait for local update finish before cleanup.

        Return:
            A bool indicates whether the local update is successful or not.
        """
        while (
            not self.otaclient_failed.is_set()
            and not self._otaclient_finish_update.is_set()
        ):
            time.sleep(self.WAIT_INTERVAL)
        return not self.otaclient_failed.is_set()


class _OTAUpdater:
    """The implementation of OTA update logic."""

    def __init__(
        self,
        boot_controller: BootControllerProtocol,
        *,
        create_standby_cls: Type[StandbySlotCreatorProtocol],
    ) -> None:
        self._boot_controller = boot_controller
        self._create_standby_cls = create_standby_cls

        # init update status
        self.update_phase: UpdatePhase = UpdatePhase.INITIALIZING
        self.update_start_time = 0
        self.updating_version: str = ""
        self.failure_reason = ""
        # init variables needed for update
        self._otameta: OTAMetadata = None  # type: ignore
        self._url_base: str = None  # type: ignore

        # init downloader
        self._downloader = Downloader()
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

    def _download_files(self, download_list: Iterator[RegularInf]):
        """Download all needed OTA image files indicated by calculated bundle."""
        logger.debug("download neede OTA image files...")

        def _download_file(entry: RegularInf) -> RegInfProcessedStats:
            """Download single OTA image file."""
            cur_stat = RegInfProcessedStats(op=RegProcessOperation.DOWNLOAD_REMOTE_COPY)
            _start_time, _download_time = time.thread_time_ns(), 0

            _fhash_str = entry.get_hash()
            _local_copy = self._ota_tmp_on_standby / _fhash_str
            entry_url, compression_alg = self._otameta.get_download_url(entry)
            (
                cur_stat.download_errors,
                cur_stat.downloaded_bytes,
                _download_time,
            ) = self._downloader.download(
                entry_url,
                _local_copy,
                digest=_fhash_str,
                size=entry.size,
                compression_alg=compression_alg,
            )
            cur_stat.size = _local_copy.stat().st_size
            cur_stat.elapsed_ns = time.thread_time_ns() - _start_time + _download_time
            return cur_stat

        _keep_failing_timer = time.time()
        with ThreadPoolExecutor(thread_name_prefix="downloading") as _executor:
            _mapper = RetryTaskMap(
                _download_file,
                download_list,
                title="downloading_ota_files",
                max_concurrent=cfg.MAX_CONCURRENT_DOWNLOAD_TASKS,
                backoff_max=cfg.DOWNLOAD_GROUP_BACKOFF_MAX,
                backoff_factor=cfg.DOWNLOAD_GROUP_BACKOFF_FACTOR,
                max_retry=0,  # NOTE: we use another strategy below
                executor=_executor,
            )
            for _exp, _entry, _stats in _mapper.execute():
                # task successfully finished
                if not isinstance(_exp, Exception) and _stats:
                    self._update_stats_collector.report(_stats)
                    # reset the failing timer on one succeeded task
                    _keep_failing_timer = time.time()
                    continue

                # task failed
                # NOTE: for failed task, it must has retried <DOWNLOAD_RETRY>
                #       time, so we manually create one download report
                logger.debug(f"failed to download {_entry=}: {_exp!r}")
                self._update_stats_collector.report(
                    RegInfProcessedStats(
                        op=RegProcessOperation.DOWNLOAD_ERROR_REPORT,
                        download_errors=cfg.DOWNLOAD_RETRY,
                    )
                )
                # task group keeps failing longer than limit,
                # shutdown the task group and raise the exception
                if (
                    time.time() - _keep_failing_timer
                    > cfg.DOWNLOAD_GROUP_NO_SUCCESS_RETRY_TIMEOUT
                ):
                    _mapper.shutdown()

        # all tasks are finished, waif for stats collector to finish processing
        # all the reported stats
        self._update_stats_collector.wait_staging()

    # update steps

    def _apply_update(self):
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
        self.update_phase = UpdatePhase.CALCULATING_DELTA
        self._standby_slot_creator = self._create_standby_cls(
            ota_metadata=self._otameta,
            boot_dir=str(self._boot_controller.get_standby_boot_dir()),
            standby_slot_mount_point=cfg.MOUNT_POINT,
            active_slot_mount_point=cfg.ACTIVE_ROOT_MOUNT_POINT,
            stats_collector=self._update_stats_collector,
        )
        try:
            _delta_bundle = self._standby_slot_creator.calculate_and_prepare_delta()
        except Exception as e:
            logger.error(f"failed to generate delta: {e!r}")
            raise UpdateDeltaGenerationFailed from e

        # --- download needed files --- #
        logger.info(
            "start to download needed files..."
            f"total_download_files_size={_delta_bundle.total_download_files_size:,}bytes"
        )
        self.update_phase = UpdatePhase.DOWNLOADING_OTA_FILES
        try:
            self._download_files(_delta_bundle.get_download_list())
        except DownloadFailedSpaceNotEnough:
            logger.critical("not enough space is left on standby slot")
            raise StandbySlotSpaceNotEnoughError from None
        except Exception as e:
            logger.error(f"failed to finish downloading files: {e!r}")
            raise NetworkError from e

        # ------ in_update ------ #
        logger.info("start to apply changes to standby slot...")
        self.update_phase = UpdatePhase.APPLYING_UPDATE
        try:
            self._standby_slot_creator.create_standby_slot()
        except Exception as e:
            logger.error(f"failed to apply update to standby slot: {e!r}")
            raise ApplyOTAUpdateFailed from e
        logger.info("finished updating standby slot")

    # API

    def shutdown(self):
        self.update_phase = None
        self._downloader.shutdown()
        self._update_stats_collector.stop()

    def update_progress(self) -> Tuple[str, wrapper.StatusProgress]:
        """
        Returns:
            A tuple contains the version and the update_progress.
        """
        update_progress = self._update_stats_collector.get_snapshot()
        # set update phase
        if self.update_phase is not None:
            update_progress.phase = self.update_phase
        # set total_elapsed_time
        update_progress.total_elapsed_time = wrapper.Duration.from_nanoseconds(
            time.time_ns() - self.update_start_time
        )

        return self.updating_version, update_progress

    def execute(
        self,
        version: str,
        raw_url_base: str,
        cookies_json: str,
        *,
        fsm: OTAUpdateFSM,
    ):
        """Main entry for ota-update.


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

        self.update_phase = wrapper.StatusProgressPhase.INITIAL
        self._update_stats_collector.start()
        try:
            # ------ parse url_base ------ #
            # unconditionally regulate the url_base
            _url_base = urlparse(raw_url_base)
            _path = f"{_url_base.path.rstrip('/')}/"
            self._url_base = _url_base._replace(path=_path).geturl()

            # ------ parse cookies ------ #
            logger.debug("process cookies_json...")
            try:
                _cookies = json.loads(cookies_json)
                assert isinstance(
                    _cookies, dict
                ), f"invalid cookies, expecting json object: {cookies_json}"
                self._downloader.configure_cookies(_cookies)
            except (JSONDecodeError, AssertionError) as e:
                raise InvalidUpdateRequest from e

            # ------ configure proxy ------ #
            logger.debug("configure proxy setting...")
            if proxy := proxy_cfg.get_proxy_for_local_ota():
                # wait for stub to setup the local ota_proxy server
                if not fsm.client_wait_for_ota_proxy():
                    raise OTAProxyFailedToStart("ota_proxy failed to start, abort")
                # NOTE(20221013): check requests document for how to set proxy,
                #                 we only support using http proxy here.
                logger.debug(f"use {proxy=} for local OTA update")
                self._downloader.configure_proxies({"http": proxy})

            # ------ process metadata.jwt and ota metafiles ------ #
            logger.debug("process metadata.jwt...")
            self.update_phase = wrapper.StatusProgressPhase.METADATA
            try:
                self._otameta = OTAMetadata(
                    url_base=self._url_base,
                    downloader=self._downloader,
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

            if total_regular_file_size := self._otameta.total_regular_size:
                self._update_stats_collector.set_total_regular_files_size(
                    total_regular_file_size
                )

            # ------ enter update ------ #
            logger.info("enter local OTA update...")
            self._apply_update()
            # local update finished, set the status to POST_PROCESSING
            fsm.client_finish_update()

    def get_update_status(self) -> UpdateStatus:
        """
        Returns:
            A tuple contains the version and the update_progress.
        """
        update_progress = self._update_stats_collector.get_snapshot()
        # update static information
        update_progress.update_start_timestamp = self.update_start_time
        update_progress.update_firmware_version = self.updating_version
        if self._otameta:
            update_progress.total_image_size = self._otameta.total_image_size
            update_progress.total_files_num = self._otameta.total_files_num
        # update other information
        update_progress.phase = self.update_phase
        update_progress.total_elapsed_time = wrapper.Duration.from_nanoseconds(
            time.time_ns() - self.update_start_time
        )
        return update_progress
        except OTAError as e:
            logger.error(f"update failed: {e!r}")
            self._boot_controller.on_operation_failure()
            raise OTAUpdateError(e) from e
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

    def __init__(
        self,
        *,
        boot_control_cls: Type[BootControllerProtocol],
        create_standby_cls: Type[StandbySlotCreatorProtocol],
        my_ecu_id: str = "",
    ):
        self.my_ecu_id = my_ecu_id
        # ensure only one update/rollback session is running
        self._lock = threading.Lock()

        self.boot_controller = boot_control_cls()
        self.create_standby_cls = create_standby_cls
        self.live_ota_status = LiveOTAStatus(self.boot_controller.get_ota_status())

        self.current_version = self.boot_controller.load_version()
        self.last_failure: Optional[OTA_APIError] = None

        # executors for update/rollback
        self._update_executor: _OTAUpdater = None  # type: ignore
        self._rollback_executor: _OTARollbacker = None  # type: ignore

        # err record
        self.last_failure_type = wrapper.FailureType.NO_FAILURE
        self.last_failure_reason = ""

    # API

    def update(
        self,
        version: str,
        url_base: str,
        cookies_json: str,
        *,
        fsm: OTAUpdateFSM,
    ):
        if self._lock.acquire(blocking=False):
            try:
                logger.info("[update] entering...")
                self._update_executor = _OTAUpdater(
                    boot_controller=self.boot_controller,
                    create_standby_cls=self.create_standby_cls,
                )
                self.last_failure_type = wrapper.FailureType.NO_FAILURE
                self.last_failure_reason = ""
                self.live_ota_status.set_ota_status(wrapper.StatusOta.UPDATING)
                self._update_executor.execute(version, url_base, cookies_json, fsm=fsm)
            except OTAUpdateError as e:
                self.live_ota_status.set_ota_status(wrapper.StatusOta.FAILURE)
                self.last_failure_type = e.get_err_type()
                self.last_failure_reason = e.get_err_reason()
                fsm.on_otaclient_failed()
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
                self.live_ota_status.set_ota_status(wrapper.StatusOta.ROLLBACKING)
                self._rollback_executor.execute()
            # silently ignore overlapping request
            except OTARollbackError as e:
                self.live_ota_status.set_ota_status(wrapper.StatusOta.ROLLBACK_FAILURE)
                self.last_failure_type = e.get_err_type()
                self.last_failure_reason = e.get_err_reason()
            finally:
                self._rollback_executor = None  # type: ignore
                self._lock.release()
        else:
            logger.warning(
                "ignore incoming rollback request as local update/rollback is ongoing"
            )

    def status(self) -> StatusResponseEcuV2:
        _live_ota_status = self.live_ota_status.get_ota_status()
        _res = StatusResponseEcuV2(
            ecu_id=self.my_ecu_id,
            firmware_version=self.current_version,
            otaclient_version=__version__,
            ota_status=_live_ota_status,
            failure_type=self.last_failure_type,
            failure_reason=self.last_failure_reason,
        )
        if _live_ota_status == wrapper.StatusOta.UPDATING and self._update_executor:
            _res.update_status = self._update_executor.get_update_status()

        return _res
