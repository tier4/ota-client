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


import json
import tempfile
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from json.decoder import JSONDecodeError
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Type
from urllib.parse import urlparse, quote

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
from .common import OTAFileCacheControl, RetryTaskMap, urljoin_ensure_base
from .configs import config as cfg
from .create_standby import StandbySlotCreatorProtocol, UpdateMeta
from .create_standby.common import DeltaBundle
from .downloader import (
    DestinationNotAvailableError,
    DownloadFailedSpaceNotEnough,
    DownloadError,
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
from .ota_metadata import MetaFile, OTAMetadata, ParseMetadataHelper, RegularInf
from .ota_status import LiveOTAStatus
from .proto import wrapper
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
        self.update_phase: wrapper.StatusProgressPhase = None  # type: ignore
        self.update_start_time = 0
        self.updating_version: str = ""
        self.failure_reason = ""
        # init variables needed for update
        self._otameta: OTAMetadata = None  # type: ignore
        self._cookies: Dict[str, Any] = None  # type: ignore
        self._url_base: str = None  # type: ignore
        self._proxy: Dict[str, Any] = None  # type: ignore

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

    # properties

    def _set_update_phase(self, _phase: wrapper.StatusProgressPhase):
        self.update_phase = _phase

    def _get_update_phase(self) -> wrapper.StatusProgressPhase:
        return self.update_phase

    # helper methods

    def _process_metadata_jwt(self) -> OTAMetadata:
        logger.debug("process metadata jwt...")
        with tempfile.TemporaryDirectory(prefix=__name__) as d:
            meta_file = Path(d) / "metadata.jwt"
            # NOTE: do not use cache when fetching metadata
            metadata_jwt_url = urljoin_ensure_base(self._url_base, "metadata.jwt")
            self._downloader.download(
                metadata_jwt_url,
                meta_file,
                cookies=self._cookies,
                proxies=self._proxy,
                headers={
                    OTAFileCacheControl.header_lower.value: OTAFileCacheControl.no_cache.value
                },
            )
            _parser = ParseMetadataHelper(
                meta_file.read_text(), certs_dir=cfg.CERTS_DIR
            )
            metadata = _parser.get_otametadata()

            # download certificate and verify metadata against this certificate
            cert_info = metadata.certificate
            cert_fname, cert_hash = cert_info.file, cert_info.hash
            cert_file: Path = Path(d) / cert_fname
            self._downloader.download(
                urljoin_ensure_base(self._url_base, cert_fname),
                cert_file,
                digest=cert_hash,
                cookies=self._cookies,
                proxies=self._proxy,
                headers={
                    OTAFileCacheControl.header_lower.value: OTAFileCacheControl.no_cache.value
                },
            )

            _parser.verify_metadata(cert_file.read_bytes())
        # bind the processed and verified otameta to class
        self._otameta = metadata
        return metadata

    def _download_file(self, entry: RegularInf) -> RegInfProcessedStats:
        """Download single OTA image file."""
        cur_stat = RegInfProcessedStats(op=RegProcessOperation.OP_DOWNLOAD)
        _start_time, _download_time = time.thread_time_ns(), 0

        _local_copy = self._ota_tmp_on_standby / entry.sha256hash
        entry_url, compression_alg = self._otameta.get_download_url(
            entry, base_url=self._url_base
        )
        (
            cur_stat.errors,
            cur_stat.download_bytes,
            _download_time,
        ) = self._downloader.download(
            entry_url,
            _local_copy,
            digest=entry.sha256hash,
            size=entry.size,
            proxies=self._proxy,
            cookies=self._cookies,
            compression_alg=compression_alg,
        )
        cur_stat.size = _local_copy.stat().st_size
        cur_stat.elapsed_ns = time.thread_time_ns() - _start_time + _download_time
        return cur_stat

    def _download_files(self, delta_bundle: DeltaBundle):
        """Download all needed OTA image files indicated by calculated bundle."""
        logger.debug("download neede OTA image files...")
        _keep_failing_timer = time.time()
        with ThreadPoolExecutor(thread_name_prefix="downloading") as _executor:
            _mapper = RetryTaskMap(
                self._download_file,
                delta_bundle.download_list,
                title="downloading_ota_files",
                max_concurrent=cfg.MAX_CONCURRENT_TASKS,
                backoff_max=cfg.DOWNLOAD_GROUP_BACKOFF_MAX,
                backoff_factor=cfg.DOWNLOAD_GROUP_BACKOFF_FACTOR,
                max_retry=0,  # NOTE: we use another strategy below
                executor=_executor,
            )
            for _exp, _entry, _stats in _mapper.execute():
                # task successfully finished
                if not isinstance(_exp, Exception):
                    self._update_stats_collector.report(_stats)
                    # reset the failing timer on one succeeded task
                    _keep_failing_timer = time.time()
                    continue

                # task failed
                # NOTE: for failed task, a RegInfoProcessStats that have
                #       OP_DOWNLOAD_FAILED_REPORT op will be returned
                logger.debug(f"failed to download {_entry=}: {_exp!r}")
                self._update_stats_collector.report(
                    RegInfProcessedStats(
                        op=RegProcessOperation.OP_DOWNLOAD_FAILED_REPORT,
                        errors=cfg.DOWNLOAD_RETRY,
                    )
                )
                # task group keeps failing longer than limit,
                # shutdown the task group and raise the exception
                if (
                    time.time() - _keep_failing_timer
                    > cfg.DOWNLOAD_GROUP_NO_SUCCESS_RETRY_TIMEOUT
                ):
                    _mapper.shutdown(raise_last_exp=True)

        # all tasks are finished, waif for stats collector to finish processing
        # all the reported stats
        self._update_stats_collector.wait_staging()

    def _download_otameta_file(self, _meta_file: MetaFile):
        meta_f_url = urljoin_ensure_base(self._url_base, quote(_meta_file.file))
        self._downloader.download(
            meta_f_url,
            self._ota_tmp_image_meta_dir_on_standby / _meta_file.file,
            digest=_meta_file.hash,
            proxies=self._proxy,
            cookies=self._cookies,
            headers={
                OTAFileCacheControl.header_lower.value: OTAFileCacheControl.no_cache.value
            },
        )

    def _download_otameta_files(self):
        logger.debug("download OTA image metafiles...")
        _keep_failing_timer = time.time()
        with ThreadPoolExecutor(thread_name_prefix="downloading_otameta") as _executor:
            _mapper = RetryTaskMap(
                self._download_otameta_file,
                self._otameta.get_img_metafiles(),
                title="downloading_otameta_files",
                max_concurrent=cfg.MAX_CONCURRENT_TASKS,
                backoff_max=cfg.DOWNLOAD_GROUP_BACKOFF_MAX,
                backoff_factor=cfg.DOWNLOAD_GROUP_BACKOFF_FACTOR,
                max_retry=0,  # NOTE: we use another strategy below
                executor=_executor,
            )
            for _exp, _entry, _ in _mapper.execute():
                if not isinstance(_exp, Exception):
                    _keep_failing_timer = time.time()
                    continue

                logger.debug(f"metafile downloading failed: {_entry=}")
                if (
                    time.time() - _keep_failing_timer
                    > cfg.DOWNLOAD_GROUP_NO_SUCCESS_RETRY_TIMEOUT
                ):
                    _mapper.shutdown(raise_last_exp=True)

    # update steps

    def _apply_update(self):
        """Apply OTA update to standby slot."""
        # ------ pre_update ------ #
        # --- prepare standby slot --- #
        # NOTE: erase standby slot or not based on the used StandbySlotCreator
        logger.debug("boot controller prepares standby slot...")
        self._boot_controller.pre_update(
            self.updating_version,
            standby_as_ref=False,  # NOTE: deprecate this option
            erase_standby=self._create_standby_cls.should_erase_standby_slot(),
        )
        # prepare the tmp storage on standby slot after boot_controller.pre_update finished
        self._ota_tmp_on_standby.mkdir(exist_ok=True)
        self._ota_tmp_image_meta_dir_on_standby.mkdir(exist_ok=True)

        # --- download otameta files for the target image --- #
        logger.info("download otameta files...")
        self.update_phase = wrapper.StatusProgressPhase.METADATA
        try:
            self._download_otameta_files()
        except HashVerificaitonError as e:
            raise OTAMetaVerificationFailed from e
        except DestinationNotAvailableError as e:
            raise OTAErrorUnRecoverable from e
        except Exception as e:
            raise OTAMetaDownloadFailed from e

        # --- init standby_slot creator, calculate delta --- #
        logger.info("start to calculate and prepare delta...")
        self.update_phase = wrapper.StatusProgressPhase.REGULAR
        _updatemeta = UpdateMeta(
            metadata=self._otameta,
            boot_dir=str(self._boot_controller.get_standby_boot_dir()),
            standby_slot_mp=cfg.MOUNT_POINT,
            active_slot_mp=cfg.ACTIVE_ROOT_MOUNT_POINT,
        )
        self._standby_slot_creator = self._create_standby_cls(
            update_meta=_updatemeta,
            stats_collector=self._update_stats_collector,
            update_phase_tracker=self._set_update_phase,
        )
        try:
            _delta_bundle = self._standby_slot_creator.calculate_and_prepare_delta()
        except Exception as e:
            raise UpdateDeltaGenerationFailed from e

        # --- download needed files --- #
        logger.info(
            "start to download needed files..."
            f"total_download_files_size={_delta_bundle.total_download_files_size:,}bytes"
        )
        self.update_phase = wrapper.StatusProgressPhase.REGULAR
        try:
            self._download_files(_delta_bundle)
        except DownloadFailedSpaceNotEnough:
            raise StandbySlotSpaceNotEnoughError from None
        except Exception as e:
            raise NetworkError from e

        # ------ in_update ------ #
        logger.info("start to apply changes to standby slot...")
        try:
            self._standby_slot_creator.create_standby_slot()
        except Exception as e:
            raise ApplyOTAUpdateFailed from e
        logger.info("finished updating standby slot")

    # API

    def shutdown(self):
        self.update_phase = None  # type: ignore
        self._proxy = None  # type: ignore
        self._update_stats_collector.stop()

    def update_progress(self) -> Tuple[str, wrapper.StatusProgress]:
        """
        Returns:
            A tuple contains the version and the update_progress.
        """
        update_progress = self._update_stats_collector.get_snapshot()
        # set update phase
        if self.update_phase is not None:
            update_progress.phase = self.update_phase.value
        # set total_elapsed_time
        update_progress.total_elapsed_time.FromNanoseconds(
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
            self.update_phase = wrapper.StatusProgressPhase.METADATA
            # ------ parse url_base ------ #
            # unconditionally regulate the url_base
            _url_base = urlparse(raw_url_base)
            _path = f"{_url_base.path.rstrip('/')}/"
            self._url_base = _url_base._replace(path=_path).geturl()

            # ------ parse cookies ------ #
            logger.debug("process cookies_json...")
            try:
                self._cookies = json.loads(cookies_json)
                assert isinstance(
                    self._cookies, dict
                ), f"invalid cookies, expecting to be parsed into dict but {type(self._cookies)}"
            except (JSONDecodeError, AssertionError) as e:
                raise InvalidUpdateRequest from e

            # ------ process metadata.jwt ------ #
            logger.debug("process metadata.jwt...")
            try:
                self._otameta = self._process_metadata_jwt()
            except DownloadError as e:
                raise NetworkError("failed to download metadata") from e
            except ValueError as e:
                raise BaseOTAMetaVerificationFailed from e

            # ------ configure proxy ------ #
            logger.debug("configure proxy setting...")
            self._proxy = None  # type: ignore
            if proxy := proxy_cfg.get_proxy_for_local_ota():
                # wait for stub to setup the local ota_proxy server
                if not fsm.client_wait_for_ota_proxy():
                    raise OTAProxyFailedToStart("ota_proxy failed to start, abort")
                # NOTE(20221013): check requests document for how to set proxy,
                #                 we only support using http proxy here.
                logger.debug(f"use {proxy=} for local OTA update")
                self._proxy = {"http": proxy}

            if total_regular_file_size := self._otameta.total_regular_size:
                self._update_stats_collector.set_total_regular_files_size(
                    total_regular_file_size
                )

            # ------ enter update ------ #
            logger.info("enter local OTA update...")
            self._apply_update()
            # local update finished, set the status to POST_PROCESSING
            fsm.client_finish_update()

            # ------ post update ------ #
            self.update_phase = wrapper.StatusProgressPhase.POST_PROCESSING
            logger.info("local update finished, wait on all subecs...")
            # NOTE: still reboot event local cleanup failed as the update itself is successful
            fsm.client_wait_for_reboot()
            logger.info("enter boot control post update phase...")
            self._boot_controller.post_update()
            # NOTE: no need to call shutdown method here, keep the update_progress
            #       as it as we are still in updating before rebooting
        except OTAError as e:
            logger.error(f"update failed: {e!r}")
            self._boot_controller.on_operation_failure()
            self.shutdown()
            raise OTAUpdateError(e) from e


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
        self._update_lock = threading.Lock()
        self._rollback_lock = threading.Lock()

        self.boot_controller = boot_control_cls()
        self.create_standby_cls = create_standby_cls
        self.live_ota_status = LiveOTAStatus(self.boot_controller.get_ota_status())

        self.current_version = self.boot_controller.load_version()
        self.last_failure: Optional[OTA_APIError] = None

        # executors for update/rollback
        self._updater_executor: _OTAUpdater = None  # type: ignore

    def _rollback_executor(self):
        try:
            # enter rollback
            self.boot_controller.pre_rollback()
            # leave rollback
            self.boot_controller.post_rollback()
        except OTAError as e:
            logger.error(f"rollback failed: {e!r}")
            self.boot_controller.on_operation_failure()
            raise OTARollbackError(e) from e

    # API

    def get_last_failure(self) -> Optional[OTA_APIError]:
        return self.last_failure

    def update(
        self,
        version: str,
        url_base: str,
        cookies_json: str,
        *,
        fsm: OTAUpdateFSM,
    ):
        try:
            if self._update_lock.acquire(blocking=False):
                logger.info("[update] entering...")
                self._updater_executor = _OTAUpdater(
                    boot_controller=self.boot_controller,
                    create_standby_cls=self.create_standby_cls,
                )
                self.last_failure = None
                self.live_ota_status.set_ota_status(wrapper.StatusOta.UPDATING)
                self._updater_executor.execute(version, url_base, cookies_json, fsm=fsm)
            else:
                logger.warning(
                    "ignore incoming update request as local update is ongoing"
                )
        except OTAUpdateError as e:
            self.live_ota_status.set_ota_status(wrapper.StatusOta.FAILURE)
            self.last_failure = e
            fsm.on_otaclient_failed()
        finally:
            self._update_lock.release()

    def rollback(self):
        try:
            if self._rollback_lock.acquire(blocking=False):
                logger.info("[rollback] entering...")
                self.last_failure = None
                self.live_ota_status.set_ota_status(wrapper.StatusOta.ROLLBACKING)
                self._rollback_executor
            # silently ignore overlapping request
        except OTARollbackError as e:
            self.live_ota_status.set_ota_status(wrapper.StatusOta.ROLLBACK_FAILURE)
            self.last_failure = e
        finally:
            self._rollback_lock.release()

    def status(self) -> wrapper.StatusResponseEcu:
        if (
            self.live_ota_status.get_ota_status() == wrapper.StatusOta.UPDATING
            and self._updater_executor is not None
        ):
            _, _update_progress = self._updater_executor.update_progress()
            _status = wrapper.Status(
                version=self.current_version,
                status=self.live_ota_status.get_ota_status().value,
                progress=_update_progress.unwrap(),  # type: ignore
            )
            if _last_failure := self.last_failure:
                _status.failure = _last_failure.get_err_type().value
                _status.failure_reason = _last_failure.get_err_reason()

            return wrapper.StatusResponseEcu(
                ecu_id=self.my_ecu_id,
                result=wrapper.FailureType.NO_FAILURE.value,
                status=_status.unwrap(),  # type: ignore
            )
        else:  # no update/rollback on-going
            _status = wrapper.Status(
                version=self.current_version,
                status=self.live_ota_status.get_ota_status().value,
            )
            if _last_failure := self.last_failure:
                _status.failure = _last_failure.get_err_type().value
                _status.failure_reason = _last_failure.get_err_reason()

            return wrapper.StatusResponseEcu(
                ecu_id=self.my_ecu_id,
                result=wrapper.FailureType.NO_FAILURE.value,
                status=_status.unwrap(),  # type: ignore
            )
