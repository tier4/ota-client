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


from __future__ import annotations

import asyncio
import contextlib
import errno
import gc
import json
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from hashlib import sha256
from http import HTTPStatus
from json.decoder import JSONDecodeError
from pathlib import Path
from typing import Any, Iterator, Optional, Type
from urllib.parse import urlparse

import requests.exceptions as requests_exc

from ota_metadata.legacy import parser as ota_metadata_parser
from ota_metadata.legacy import types as ota_metadata_types
from otaclient import __version__
from otaclient_api.v2 import types as api_types
from otaclient_common.common import ensure_otaproxy_start
from otaclient_common.downloader import EMPTY_FILE_SHA256, Downloader, DownloaderPool
from otaclient_common.persist_file_handling import PersistFilesHandler
from otaclient_common.retry_task_map import ThreadPoolExecutorWithRetry

from . import errors as ota_errors
from .boot_control import BootControllerProtocol, get_boot_controller
from .configs import config as cfg
from .configs import ecu_info
from .create_standby import StandbySlotCreatorProtocol, get_standby_slot_creator
from .interface import OTAClientProtocol
from .update_stats import (
    OTAUpdateStatsCollector,
    RegInfProcessedStats,
    RegProcessOperation,
)

logger = logging.getLogger(__name__)


class LiveOTAStatus:
    def __init__(self, ota_status: api_types.StatusOta) -> None:
        self.live_ota_status = ota_status

    def get_ota_status(self) -> api_types.StatusOta:
        return self.live_ota_status

    def set_ota_status(self, _status: api_types.StatusOta):
        self.live_ota_status = _status

    def request_update(self) -> bool:
        return self.live_ota_status in [
            api_types.StatusOta.INITIALIZED,
            api_types.StatusOta.SUCCESS,
            api_types.StatusOta.FAILURE,
            api_types.StatusOta.ROLLBACK_FAILURE,
        ]

    def request_rollback(self) -> bool:
        return self.live_ota_status in [
            api_types.StatusOta.SUCCESS,
            api_types.StatusOta.ROLLBACK_FAILURE,
        ]


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


def _download_exception_handler(_fut: Future[Any]) -> bool:
    """Parse the exception raised by a downloading task.

    This handler will raise OTA Error on exceptions that cannot(should not) be
        handled by us. For handled exceptions, just let upper caller do the
        retry for us.

    Raises:
        UpdateRequestCookieInvalid on HTTP error 401 or 403,
        OTAImageInvalid on HTTP error 404,
        StandbySlotInsufficientSpace on disk space not enough.

    Returns:
        True on succeeded downloading, False on handled exceptions.
    """
    if not (exc := _fut.exception()):
        return True

    try:
        # exceptions that cannot be handled by us
        if isinstance(exc, requests_exc.HTTPError):
            http_errcode = exc.errno

            if http_errcode in [
                HTTPStatus.FORBIDDEN,
                HTTPStatus.UNAUTHORIZED,
            ]:
                raise ota_errors.UpdateRequestCookieInvalid(
                    f"download failed with critical HTTP error: {exc.errno}, {exc!r}",
                    module=__name__,
                )
            if http_errcode == HTTPStatus.NOT_FOUND:
                raise ota_errors.OTAImageInvalid(
                    f"download failed with 404 on some file(s): {exc!r}",
                    module=__name__,
                )

        if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
            raise ota_errors.StandbySlotInsufficientSpace(
                f"download failed due to space insufficient: {exc!r}",
                module=__name__,
            )

        # handled exceptions, let the upper caller do the retry
        return False
    finally:
        exc = None  # drop ref to exc instance


class _OTAUpdater:
    """The implementation of OTA update logic."""

    def __init__(
        self,
        *,
        version: str,
        raw_url_base: str,
        cookies_json: str,
        upper_otaproxy: str | None = None,
        boot_controller: BootControllerProtocol,
        create_standby_cls: Type[StandbySlotCreatorProtocol],
        control_flags: OTAClientControlFlags,
    ) -> None:
        # ------ define OTA temp paths ------ #
        self._ota_tmp_on_standby = Path(cfg.MOUNT_POINT) / Path(
            cfg.OTA_TMP_STORE
        ).relative_to("/")
        self._ota_tmp_image_meta_dir_on_standby = Path(cfg.MOUNT_POINT) / Path(
            cfg.OTA_TMP_META_STORE
        ).relative_to("/")

        # ------ parse cookies ------ #
        logger.debug("process cookies_json...")
        try:
            cookies = json.loads(cookies_json)
            assert isinstance(
                cookies, dict
            ), f"invalid cookies, expecting json object: {cookies_json}"
        except (JSONDecodeError, AssertionError) as e:
            _err_msg = f"cookie is invalid: {cookies_json=}"
            logger.error(_err_msg)
            raise ota_errors.InvalidUpdateRequest(_err_msg, module=__name__) from e

        # ------ parse upper proxy ------ #
        logger.debug("configure proxy setting...")
        proxies = {}
        if upper_otaproxy:
            logger.info(
                f"use {upper_otaproxy} for local OTA update, "
                f"wait for otaproxy@{upper_otaproxy} online..."
            )
            ensure_otaproxy_start(
                upper_otaproxy,
                probing_timeout=cfg.DOWNLOAD_GROUP_INACTIVE_TIMEOUT,
            )
            # NOTE(20221013): check requests document for how to set proxy,
            #                 we only support using http proxy here.
            proxies["http"] = upper_otaproxy

        # ------ init updater implementation ------ #
        self._control_flags = control_flags
        self._boot_controller = boot_controller
        self._create_standby_cls = create_standby_cls

        # ------ init update status ------ #
        self.update_phase = api_types.UpdatePhase.INITIALIZING
        self.update_start_timestamp_ns = time.time_ns()

        self.updating_version: str = version
        self.failure_reason = ""

        self.delta_calculation_started_timestamp_ns = 0
        self.delta_calculation_finished_timestamp_ns = 0

        # ------ init variables needed for update ------ #
        _url_base = urlparse(raw_url_base)
        _path = f"{_url_base.path.rstrip('/')}/"
        self.url_base = _url_base._replace(path=_path).geturl()

        # ------ dynamic update status ------ #
        self.total_files_size_uncompressed = 0
        self.total_files_num = 0
        self.total_download_files_num = 0
        self.total_download_fiies_size = 0
        self.total_remove_files_num = 0

        # ------ setup OTA files downloader ------ #
        self.download_started_timestamp_ns = 0
        self.download_finished_timestamp_ns = 0

        self._downloader_pool = DownloaderPool(
            instance_num=cfg.MAX_DOWNLOAD_THREAD,
            hash_func=sha256,
            chunk_size=cfg.CHUNK_SIZE,
            cookies=cookies,
            proxies=proxies,
        )
        self._downloader_mapper: dict[int, Downloader] = {}
        self._download_watchdog_previous_active_timestamp = 0
        self._download_watchdog_previous_downloaded_bytes = 0

        # start ota update statics collector
        self._update_stats_collector = OTAUpdateStatsCollector()
        self._update_stats_collector.start()

    def _downloader_pool_watchdog(self):
        if self.download_started_timestamp_ns == 0:
            return  # download not yet started

        current_tiemstamp = int(time.time())
        if self._download_watchdog_previous_active_timestamp == 0:
            self._download_watchdog_previous_active_timestamp = current_tiemstamp
            return

        downloaded_bytes = self._downloader_pool.total_downloaded_bytes
        if downloaded_bytes > self._download_watchdog_previous_downloaded_bytes:
            self._download_watchdog_previous_downloaded_bytes = downloaded_bytes
            self._download_watchdog_previous_active_timestamp = current_tiemstamp
            return

        if (
            current_tiemstamp - self._download_watchdog_previous_active_timestamp
            > cfg.DOWNLOAD_GROUP_INACTIVE_TIMEOUT
        ):
            _err_msg = f"downloader stuck for {cfg.DOWNLOAD_GROUP_INACTIVE_TIMEOUT=} seconds, abort"
            logger.error(_err_msg)
            raise ValueError(_err_msg)

    def _download_file(
        self, entry: ota_metadata_types.RegularInf
    ) -> tuple[int, int, int]:
        """Download a single OTA image file.

        This is the single task being executed in the downloader pool.

        Returns:
            Retry counts, downloaded files size and traffic on wire.
        """
        _fhash_str = entry.get_hash()
        # special treatment to empty file
        if _fhash_str == EMPTY_FILE_SHA256:
            return 0, 0, 0

        entry_url, compression_alg = self._otameta.get_download_url(entry)
        downloader = self._downloader_mapper[threading.get_native_id()]
        return downloader.download(
            entry_url,
            self._ota_tmp_on_standby / _fhash_str,
            digest=_fhash_str,
            size=entry.size,
            compression_alg=compression_alg,
        )

    def _download_files(self, download_list: Iterator[ota_metadata_types.RegularInf]):
        """Download all needed OTA image files indicated by calculated bundle."""

        logger.debug("download neede OTA image files...")
        # special treatment to empty file, create it first
        _empty_file = self._ota_tmp_on_standby / EMPTY_FILE_SHA256
        _empty_file.touch()

        # ------ start the downloading ------ #
        def _thread_initializer():
            self._downloader_mapper[threading.get_native_id()] = (
                self._downloader_pool.get_instance()
            )

        with ThreadPoolExecutorWithRetry(
            max_concurrent=cfg.MAX_CONCURRENT_DOWNLOAD_TASKS,
            max_workers=cfg.MAX_DOWNLOAD_THREAD,
            thread_name_prefix="download_ota_files",
            initializer=_thread_initializer,
            watchdog_func=self._downloader_pool_watchdog,
        ) as _mapper:
            for _fut in _mapper.ensure_tasks(self._download_file, download_list):
                if _download_exception_handler(_fut):  # donwload succeeded
                    err_count, file_size, _ = _fut.result()
                    self._update_stats_collector.report_download_ota_files(
                        RegInfProcessedStats(
                            op=RegProcessOperation.DOWNLOAD_REMOTE_COPY,
                            download_errors=err_count,
                            size=file_size,
                        )
                    )
                else:  # download failed, but exceptions can be handled
                    self._update_stats_collector.report_download_ota_files(
                        RegInfProcessedStats(
                            op=RegProcessOperation.DOWNLOAD_ERROR_REPORT,
                            download_errors=1,
                        ),
                    )

        # all tasks are finished, waif for stats collector to finish processing
        # all the reported stats
        self._update_stats_collector.wait_staging()

        # release the downloader instances
        self._downloader_pool.release_all_instances()

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
        self.delta_calculation_started_timestamp_ns = time.time_ns()

        self.update_phase = api_types.UpdatePhase.CALCULATING_DELTA
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

        self.delta_calculation_finished_timestamp_ns = time.time_ns()

        # --- download needed files --- #
        logger.info(
            "start to download needed files..."
            f"total_download_files_size={_delta_bundle.total_download_files_size:,}bytes"
        )
        self.update_phase = api_types.UpdatePhase.DOWNLOADING_OTA_FILES
        self.download_started_timestamp_ns = time.time_ns()
        try:
            self._download_files(_delta_bundle.get_download_list())
        except ota_errors.OTAError:
            raise  # no need to wrap OTA Error again
        except Exception as e:
            _err_msg = f"failed to finish downloading files: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.NetworkError(_err_msg, module=__name__) from e

        # shutdown downloader on download finished
        self.download_finished_timestamp_ns = time.time_ns()
        self._downloader_pool.shutdown()

        # ------ in_update ------ #
        logger.info("start to apply changes to standby slot...")
        self.update_phase = api_types.UpdatePhase.APPLYING_UPDATE
        try:
            self._standby_slot_creator.create_standby_slot()
        except Exception as e:
            _err_msg = f"failed to apply update to standby slot: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.ApplyOTAUpdateFailed(_err_msg, module=__name__) from e

        logger.info("finished updating standby slot")

    def _process_persistents(self):
        logger.info("start persist files handling...")
        standby_slot_mp = Path(cfg.MOUNT_POINT)

        _handler = PersistFilesHandler(
            src_passwd_file=Path(cfg.PASSWD_FILE),
            src_group_file=Path(cfg.GROUP_FILE),
            dst_passwd_file=Path(standby_slot_mp / "etc/passwd"),
            dst_group_file=Path(standby_slot_mp / "etc/group"),
            src_root=cfg.ACTIVE_ROOT_MOUNT_POINT,
            dst_root=cfg.MOUNT_POINT,
        )

        for _perinf in self._otameta.iter_metafile(
            ota_metadata_parser.MetafilesV1.PERSISTENT_FNAME
        ):
            _per_fpath = Path(_perinf.path)

            # NOTE(20240520): with update_swapfile ansible role being used wildly,
            #   now we just ignore the swapfile entries in the persistents.txt if any,
            #   and issue a warning about it.
            if str(_per_fpath) in ["/swapfile", "/swap.img"]:
                logger.warning(
                    f"swapfile entry {_per_fpath} is listed in persistents.txt, ignored"
                )
                logger.warning(
                    (
                        "using persis file feature to preserve swapfile is MISUSE of persist file handling feature!"
                        "please change your OTA image build setting and remove swapfile entries from persistents.txt!"
                    )
                )
                continue

            if (
                _per_fpath.is_file() or _per_fpath.is_dir() or _per_fpath.is_symlink()
            ):  # NOTE: not equivalent to perinf.path.exists()
                _handler.preserve_persist_entry(_per_fpath)

    def _execute_update(self):
        """OTA update workflow implementation.

        e.g.
        cookies = {
            "CloudFront-Policy": "eyJTdGF0ZW1lbnQ...",
            "CloudFront-Signature": "o4ojzMrJwtSIg~izsy...",
            "CloudFront-Key-Pair-Id": "K2...",
        }
        """
        logger.info(f"execute local update: {self.updating_version=},{self.url_base=}")

        # ------ init, processing metadata ------ #
        self.update_phase = api_types.UpdatePhase.PROCESSING_METADATA

        # process metadata.jwt and ota metafiles
        logger.debug("process metadata.jwt...")
        downloader = self._downloader_pool.get_instance()
        try:
            # TODO(20240619): ota_metadata should not be responsible for downloading anything
            self._otameta = ota_metadata_parser.OTAMetadata(
                url_base=self.url_base,
                downloader=downloader,
                run_dir=Path(cfg.RUN_DIR),
                certs_dir=Path(cfg.CERTS_DIR),
            )
            self.total_files_num = self._otameta.total_files_num
            self.total_files_size_uncompressed = (
                self._otameta.total_files_size_uncompressed
            )
        except ota_metadata_parser.MetadataJWTVerificationFailed as e:
            _err_msg = f"failed to verify metadata.jwt: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.MetadataJWTVerficationFailed(
                _err_msg, module=__name__
            ) from e
        except ota_metadata_parser.MetadataJWTPayloadInvalid as e:
            _err_msg = f"metadata.jwt is invalid: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.MetadataJWTInvalid(_err_msg, module=__name__) from e
        except Exception as e:
            _err_msg = f"failed to prepare ota metafiles: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.OTAMetaDownloadFailed(_err_msg, module=__name__) from e
        finally:
            self._downloader_pool.release_instance()

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
        logger.info("enter post update phase...")
        self.update_phase = api_types.UpdatePhase.PROCESSING_POSTUPDATE
        # NOTE(20240219): move persist file handling here
        self._process_persistents()

        # boot controller postupdate
        next(_postupdate_gen := self._boot_controller.post_update())

        logger.info("local update finished, wait on all subecs...")
        self._control_flags.wait_can_reboot_flag()
        next(_postupdate_gen, None)  # reboot

    # API

    def shutdown(self):
        self.update_phase = api_types.UpdatePhase.INITIALIZING
        self._downloader_pool.shutdown()
        self._update_stats_collector.stop()

    def get_update_status(self) -> api_types.UpdateStatus:
        """
        Returns:
            A tuple contains the version and the update_progress.
        """
        current_timestamp_ns = time.time_ns()

        update_progress = self._update_stats_collector.get_snapshot()
        # update static information
        # NOTE: timestamp is in seconds
        update_progress.update_start_timestamp = (
            self.update_start_timestamp_ns // 1_000_000_000
        )
        update_progress.update_firmware_version = self.updating_version

        # update dynamic information
        update_progress.total_files_size_uncompressed = (
            self.total_files_size_uncompressed
        )
        update_progress.total_files_num = self.total_files_num
        update_progress.total_download_files_num = self.total_download_files_num
        update_progress.total_download_files_size = self.total_download_fiies_size
        update_progress.total_remove_files_num = self.total_remove_files_num

        # delta generation stats
        if self.delta_calculation_started_timestamp_ns == 0:
            update_progress.delta_generating_elapsed_time = (
                api_types.Duration.from_nanoseconds(0)
            )
        elif self.delta_calculation_finished_timestamp_ns != 0:
            update_progress.delta_generating_elapsed_time = (
                api_types.Duration.from_nanoseconds(
                    self.delta_calculation_finished_timestamp_ns
                    - self.delta_calculation_started_timestamp_ns
                )
            )
        else:
            update_progress.delta_generating_elapsed_time = (
                api_types.Duration.from_nanoseconds(
                    current_timestamp_ns - self.delta_calculation_started_timestamp_ns
                    if self.delta_calculation_started_timestamp_ns
                    else 0
                )
            )

        # downloading stats
        update_progress.downloaded_bytes = self._downloader_pool.total_downloaded_bytes
        if self.download_started_timestamp_ns == 0:
            update_progress.downloading_elapsed_time = (
                api_types.Duration.from_nanoseconds(0)
            )
        elif self.download_finished_timestamp_ns != 0:
            update_progress.downloading_elapsed_time = (
                api_types.Duration.from_nanoseconds(
                    self.download_finished_timestamp_ns
                    - self.download_started_timestamp_ns
                )
            )
        else:
            update_progress.downloading_elapsed_time = (
                api_types.Duration.from_nanoseconds(
                    current_timestamp_ns - self.download_started_timestamp_ns
                )
            )

        # update other information
        update_progress.phase = self.update_phase
        update_progress.total_elapsed_time = api_types.Duration.from_nanoseconds(
            current_timestamp_ns - self.update_start_timestamp_ns
            if self.update_start_timestamp_ns
            else 0
        )
        return update_progress

    def execute(self) -> None:
        """Main entry for executing local OTA update.

        Handles OTA failure and logging/finalizing on failure.
        """
        try:
            self._execute_update()
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
            self._update_executor: _OTAUpdater | None = None
            self._rollback_executor: _OTARollbacker | None = None

            # err record
            self.last_failure_type = api_types.FailureType.NO_FAILURE
            self.last_failure_reason = ""
            self.last_failure_traceback = ""
        except Exception as e:
            _err_msg = f"failed to start otaclient core: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.OTAClientStartupFailed(_err_msg, module=__name__) from e

    def _on_failure(self, exc: ota_errors.OTAError, ota_status: api_types.StatusOta):
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
            del exc  # prevent ref cycle

    # API

    def update(self, version: str, url_base: str, cookies_json: str) -> None:
        if self._lock.acquire(blocking=False):
            try:
                logger.info("[update] entering local update...")
                self._update_executor = _OTAUpdater(
                    version=version,
                    raw_url_base=url_base,
                    cookies_json=cookies_json,
                    boot_controller=self.boot_controller,
                    create_standby_cls=self.create_standby_cls,
                    control_flags=self.control_flags,
                    upper_otaproxy=self.proxy,
                )

                self.last_failure_type = api_types.FailureType.NO_FAILURE
                self.last_failure_reason = ""
                self.last_failure_traceback = ""

                self.live_ota_status.set_ota_status(api_types.StatusOta.UPDATING)
                self._update_executor.execute()
            except ota_errors.OTAError as e:
                self._on_failure(e, api_types.StatusOta.FAILURE)
            finally:
                self._update_executor = None
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
                self.last_failure_type = api_types.FailureType.NO_FAILURE
                self.last_failure_reason = ""
                self.last_failure_traceback = ""

                # entering rollback
                self.live_ota_status.set_ota_status(api_types.StatusOta.ROLLBACKING)
                self._rollback_executor.execute()
            # silently ignore overlapping request
            except ota_errors.OTAError as e:
                self._on_failure(e, api_types.StatusOta.ROLLBACK_FAILURE)
            finally:
                self._rollback_executor = None  # type: ignore
                self._lock.release()
        else:
            logger.warning(
                "ignore incoming rollback request as local update/rollback is ongoing"
            )

    def status(self) -> api_types.StatusResponseEcuV2:
        live_ota_status = self.live_ota_status.get_ota_status()
        status_report = api_types.StatusResponseEcuV2(
            ecu_id=self.my_ecu_id,
            firmware_version=self.current_version,
            otaclient_version=self.OTACLIENT_VERSION,
            ota_status=live_ota_status,
            failure_type=self.last_failure_type,
            failure_reason=self.last_failure_reason,
            failure_traceback=self.last_failure_traceback,
        )
        if live_ota_status == api_types.StatusOta.UPDATING and self._update_executor:
            status_report.update_status = self._update_executor.get_update_status()
        return status_report


class OTAServicer:
    def __init__(
        self,
        *,
        control_flags: OTAClientControlFlags,
        executor: Optional[ThreadPoolExecutor] = None,
        otaclient_version: str = __version__,
        proxy: Optional[str] = None,
    ) -> None:
        self.ecu_id = ecu_info.ecu_id
        self.otaclient_version = otaclient_version
        self.local_used_proxy_url = proxy
        self.last_operation: Optional[api_types.StatusOta] = None

        # default boot startup failure if boot_controller/otaclient_core crashed without
        # raising specific error
        self._otaclient_startup_failed_status = api_types.StatusResponseEcuV2(
            ecu_id=ecu_info.ecu_id,
            otaclient_version=otaclient_version,
            ota_status=api_types.StatusOta.FAILURE,
            failure_type=api_types.FailureType.UNRECOVERABLE,
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
        _bootctrl_cls = get_boot_controller(ecu_info.bootloader)
        _standby_slot_creator = get_standby_slot_creator(cfg.STANDBY_CREATION_MODE)

        # boot controller starts up
        try:
            _bootctrl_inst = _bootctrl_cls()
        except ota_errors.OTAError as e:
            logger.error(
                e.get_error_report(title=f"boot controller startup failed: {e!r}")
            )
            self._otaclient_startup_failed_status = api_types.StatusResponseEcuV2(
                ecu_id=ecu_info.ecu_id,
                otaclient_version=otaclient_version,
                ota_status=api_types.StatusOta.FAILURE,
                failure_type=api_types.FailureType.UNRECOVERABLE,
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
            self._otaclient_startup_failed_status = api_types.StatusResponseEcuV2(
                ecu_id=ecu_info.ecu_id,
                otaclient_version=otaclient_version,
                ota_status=api_types.StatusOta.FAILURE,
                failure_type=api_types.FailureType.UNRECOVERABLE,
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
        self, request: api_types.UpdateRequestEcu
    ) -> api_types.UpdateResponseEcu:
        # prevent update operation if otaclient is not started
        if self._otaclient_inst is None:
            return api_types.UpdateResponseEcu(
                ecu_id=self.ecu_id, result=api_types.FailureType.UNRECOVERABLE
            )

        # check and acquire lock
        if self._update_rollback_lock.locked():
            logger.warning(
                f"ongoing operation: {self.last_operation=}, ignore incoming {request=}"
            )
            return api_types.UpdateResponseEcu(
                ecu_id=self.ecu_id, result=api_types.FailureType.RECOVERABLE
            )

        # immediately take the lock if not locked
        await self._update_rollback_lock.acquire()
        self.last_operation = api_types.StatusOta.UPDATING

        async def _update_task():
            if self._otaclient_inst is None:
                return

            # error should be collected by otaclient, not us
            with contextlib.suppress(Exception):
                await self._run_in_executor(
                    partial(
                        self._otaclient_inst.update,
                        request.version,
                        request.url,
                        request.cookies,
                    )
                )
            self.last_operation = None
            self._update_rollback_lock.release()

        # dispatch update to background
        asyncio.create_task(_update_task())

        return api_types.UpdateResponseEcu(
            ecu_id=self.ecu_id, result=api_types.FailureType.NO_FAILURE
        )

    async def dispatch_rollback(
        self, request: api_types.RollbackRequestEcu
    ) -> api_types.RollbackResponseEcu:
        # prevent rollback operation if otaclient is not started
        if self._otaclient_inst is None:
            return api_types.RollbackResponseEcu(
                ecu_id=self.ecu_id, result=api_types.FailureType.UNRECOVERABLE
            )

        # check and acquire lock
        if self._update_rollback_lock.locked():
            logger.warning(
                f"ongoing operation: {self.last_operation=}, ignore incoming {request=}"
            )
            return api_types.RollbackResponseEcu(
                ecu_id=self.ecu_id, result=api_types.FailureType.RECOVERABLE
            )

        # immediately take the lock if not locked
        await self._update_rollback_lock.acquire()
        self.last_operation = api_types.StatusOta.ROLLBACKING

        async def _rollback_task():
            if self._otaclient_inst is None:
                return

            # error should be collected by otaclient, not us
            with contextlib.suppress(Exception):
                await self._run_in_executor(self._otaclient_inst.rollback)
            self.last_operation = None
            self._update_rollback_lock.release()

        # dispatch to background
        asyncio.create_task(_rollback_task())

        return api_types.RollbackResponseEcu(
            ecu_id=self.ecu_id, result=api_types.FailureType.NO_FAILURE
        )

    async def get_status(self) -> api_types.StatusResponseEcuV2:
        # otaclient is not started due to boot control startup failed
        if self._otaclient_inst is None:
            return self._otaclient_startup_failed_status

        # otaclient core started, query status from it
        return await self._run_in_executor(self._otaclient_inst.status)
