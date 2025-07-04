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

import errno
import json
import logging
import multiprocessing.queues as mp_queue
import os
import shutil
import threading
import time
from concurrent.futures import Future
from dataclasses import replace
from functools import partial
from hashlib import sha256
from http import HTTPStatus
from json.decoder import JSONDecodeError
from pathlib import Path
from queue import Empty, Queue
from tempfile import TemporaryDirectory
from typing import Any, Callable, NoReturn, Optional
from urllib.parse import urlparse

import requests.exceptions as requests_exc
from requests import Response

from ota_metadata.file_table.utils import find_saved_fstable, save_fstable
from ota_metadata.legacy2 import _errors as ota_metadata_error
from ota_metadata.legacy2.metadata import (
    OTAMetadata,
    ResourceMeta,
)
from ota_metadata.utils.cert_store import (
    CACertStoreInvalid,
    CAChainStore,
    load_ca_cert_chains,
)
from otaclient import errors as ota_errors
from otaclient._status_monitor import (
    OTAClientStatusCollector,
    OTAStatusChangeReport,
    OTAUpdatePhaseChangeReport,
    SetOTAClientMetaReport,
    SetUpdateMetaReport,
    StatusReport,
    UpdateProgressReport,
)
from otaclient._types import (
    ClientUpdateControlFlags,
    ClientUpdateRequestV2,
    FailureType,
    IPCRequest,
    IPCResEnum,
    IPCResponse,
    MultipleECUStatusFlags,
    OTAStatus,
    RollbackRequestV2,
    UpdatePhase,
    UpdateRequestV2,
)
from otaclient._utils import SharedOTAClientStatusWriter, get_traceback, wait_and_log
from otaclient.boot_control import BootControllerProtocol, get_boot_controller
from otaclient.client_package import OTAClientPackageDownloader
from otaclient.configs.cfg import cfg, ecu_info, proxy_info
from otaclient.create_standby.delta_gen import (
    DeltaGenParams,
    InPlaceDeltaGenFullDiskScan,
    InPlaceDeltaWithBaseFileTable,
    RebuildDeltaGenFullDiskScan,
    RebuildDeltaWithBaseFileTable,
)
from otaclient.create_standby.resume_ota import ResourceScanner
from otaclient.create_standby.update_slot import UpdateStandbySlot
from otaclient.create_standby.utils import can_use_in_place_mode
from otaclient.metrics import OTAMetricsData
from otaclient_common import EMPTY_FILE_SHA256, _env, human_readable_size, replace_root
from otaclient_common.cmdhelper import ensure_mount, ensure_umount, mount_tmpfs
from otaclient_common.common import ensure_otaproxy_start
from otaclient_common.download_info import DownloadInfo
from otaclient_common.downloader import (
    Downloader,
    DownloaderPool,
    DownloadPoolWatchdogFuncContext,
    DownloadResult,
)
from otaclient_common.persist_file_handling import PersistFilesHandler
from otaclient_common.retry_task_map import (
    TasksEnsureFailed,
    ThreadPoolExecutorWithRetry,
)

logger = logging.getLogger(__name__)

DEFAULT_STATUS_QUERY_INTERVAL = 1
WAIT_BEFORE_REBOOT = 6
DOWNLOAD_STATS_REPORT_BATCH = 300
DOWNLOAD_REPORT_INTERVAL = 1  # second

OP_CHECK_INTERVAL = 1  # second
HOLD_REQ_HANDLING_ON_ACK_REQUEST = 16  # seconds
WAIT_FOR_OTAPROXY_ONLINE = 3 * 60  # 3mins

STANDBY_SLOT_USED_SIZE_THRESHOLD = 0.8

BASE_METADATA_FOLDER = "base"
"""On standby slot temporary OTA metadata folder(/.ota-meta), `base` folder is to
hold the OTA image metadata of standby slot itself.
"""


class OTAClientError(Exception): ...


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
            _response = exc.response
            # NOTE(20241129): if somehow HTTPError doesn't contain response,
            #       don't do anything but let upper retry.
            # NOTE: bool(Response) is False when status_code != 200.
            if not isinstance(_response, Response):
                return False

            http_errcode = _response.status_code
            if http_errcode in [HTTPStatus.FORBIDDEN, HTTPStatus.UNAUTHORIZED]:
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
        del exc, _fut  # drop ref to exc instance


class _OTAUpdateOperator:
    """The base common class of OTA update logic."""

    def __init__(
        self,
        *,
        version: str,
        raw_url_base: str,
        cookies_json: str,
        session_wd: Path,
        ca_chains_store: CAChainStore,
        upper_otaproxy: str | None = None,
        ecu_status_flags: MultipleECUStatusFlags,
        status_report_queue: Queue[StatusReport],
        session_id: str,
        metrics: OTAMetricsData,
    ) -> None:
        self.update_version = version
        self.update_start_timestamp = int(time.time())
        self.session_id = session_id
        self._status_report_queue = status_report_queue
        self._metrics = metrics

        # ------ report INITIALIZING status ------ #
        status_report_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.INITIALIZING,
                    trigger_timestamp=self.update_start_timestamp,
                ),
                session_id=session_id,
            )
        )
        self._metrics.initializing_start_timestamp = self.update_start_timestamp

        status_report_queue.put_nowait(
            StatusReport(
                payload=SetUpdateMetaReport(
                    update_firmware_version=version,
                ),
                session_id=session_id,
            )
        )
        self._metrics.target_firmware_version = version

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
        self._upper_proxy = upper_otaproxy

        # ------ mount session wd as a tmpfs ------ #
        self._session_workdir = session_wd
        session_wd.mkdir(exist_ok=True, parents=True)

        # ------ init updater implementation ------ #
        self.ecu_status_flags = ecu_status_flags

        # ------ init variables needed for update ------ #
        _url_base = urlparse(raw_url_base)
        _path = f"{_url_base.path.rstrip('/')}/"
        self.url_base = _url_base._replace(path=_path).geturl()

        # ------ setup downloader ------ #
        self._download_watchdog_ctx = DownloadPoolWatchdogFuncContext(
            downloaded_bytes=0,
            previous_active_timestamp=0,
        )
        self._downloader_pool = DownloaderPool(
            instance_num=cfg.DOWNLOAD_THREADS,
            hash_func=sha256,
            chunk_size=cfg.CHUNK_SIZE,
            cookies=cookies,
            # NOTE(20221013): check requests document for how to set proxy,
            #                 we only support using http proxy here.
            proxies={"http": upper_otaproxy} if upper_otaproxy else None,
        )
        self._downloader_mapper: dict[int, Downloader] = {}

        # ------ setup OTA metadata parser ------ #
        self._ota_metadata = OTAMetadata(
            base_url=self.url_base,
            session_dir=self._session_workdir,
            ca_chains_store=ca_chains_store,
        )

    def _handle_upper_proxy(self) -> None:
        """Ensure the upper proxy is online before starting the local OTA update."""
        if _upper_proxy := self._upper_proxy:
            logger.info(
                f"use {_upper_proxy} for local OTA update, "
                f"wait for otaproxy@{_upper_proxy} online..."
            )

            # NOTE: will raise a built-in ConnnectionError at timeout
            ensure_otaproxy_start(
                _upper_proxy,
                probing_timeout=WAIT_FOR_OTAPROXY_ONLINE,
            )

    def _process_metadata(self) -> None:
        """Process the metadata.jwt file and report."""
        _current_time = int(time.time())
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.PROCESSING_METADATA,
                    trigger_timestamp=_current_time,
                ),
                session_id=self.session_id,
            )
        )
        self._metrics.processing_metadata_start_timestamp = _current_time

        try:
            logger.info("verify and download OTA image metadata ...")
            self._download_and_parse_metadata()
            _metadata_jwt = self._ota_metadata.metadata_jwt
            assert _metadata_jwt, "invalid metadata jwt"

            logger.info(
                "ota_metadata parsed finished: \n"
                f"total_regulars_num: {self._ota_metadata.total_regulars_num} \n"
                f"total_regulars_size: {_metadata_jwt.total_regular_size}"
            )

            self._status_report_queue.put_nowait(
                StatusReport(
                    payload=SetUpdateMetaReport(
                        image_file_entries=self._ota_metadata.total_regulars_num,
                        image_size_uncompressed=_metadata_jwt.total_regular_size,
                        metadata_downloaded_bytes=self._downloader_pool.total_downloaded_bytes,
                    ),
                    session_id=self.session_id,
                )
            )
            self._metrics.ota_image_total_files_size = _metadata_jwt.total_regular_size
            self._metrics.ota_image_total_regulars_num = (
                self._ota_metadata.total_regulars_num
            )
            self._metrics.ota_image_total_directories_num = (
                self._ota_metadata.total_dirs_num
            )
            self._metrics.ota_image_total_symlinks_num = (
                self._ota_metadata.total_symlinks_num
            )

        except ota_errors.OTAError:
            raise  # raise top-level OTAError as it
        except ota_metadata_error.MetadataJWTVerificationFailed as e:
            _err_msg = f"failed to verify metadata.jwt: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.MetadataJWTVerficationFailed(
                _err_msg, module=__name__
            ) from e
        except (ota_metadata_error.MetadataJWTPayloadInvalid, AssertionError) as e:
            _err_msg = f"metadata.jwt is invalid: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.MetadataJWTInvalid(_err_msg, module=__name__) from e
        except Exception as e:
            _err_msg = f"failed to prepare ota metafiles: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.OTAMetaDownloadFailed(_err_msg, module=__name__) from e
        finally:
            self._downloader_pool.release_instance()

    def _download_and_parse_metadata(self) -> None:
        self._download_and_process_file_with_condition(
            thread_name_prefix="download_metadata_files",
            get_downloads_generator=self._ota_metadata.download_metafiles,
        )

    def _download_and_process_file_with_condition(
        self,
        thread_name_prefix: str,
        get_downloads_generator: Callable,
    ) -> None:
        """
        Download and process a list of files with a condition.
        Each file downloading and processing are done in parallel by multiple threads.
        This method is supposed to be used for downloading metadata and client files.
        """

        self._download_watchdog_ctx["previous_active_timestamp"] = int(time.time())
        _mapper = ThreadPoolExecutorWithRetry(
            max_concurrent=cfg.MAX_CONCURRENT_DOWNLOAD_TASKS,
            max_workers=cfg.DOWNLOAD_THREADS,
            max_retry_on_entry=cfg.MAX_RETRY_ON_ENTRY_COUNT,
            thread_name_prefix=thread_name_prefix,
            initializer=self._downloader_worker_initializer,
            watchdog_func=partial(
                self._downloader_pool.downloading_watchdog,
                ctx=self._download_watchdog_ctx,
                max_idle_timeout=cfg.DOWNLOAD_INACTIVE_TIMEOUT,
            ),
        )

        _condition = threading.Condition()
        _generator = get_downloads_generator(_condition)

        try:
            for _fut in _mapper.ensure_tasks(
                partial(self._download_file_with_condition, condition=_condition),
                _generator,
            ):
                if not (_exc := _fut.exception()):
                    continue

                logger.warning(f"failed to download one file, keep retrying: {_exc!r}")
                if isinstance(_exc, requests_exc.HTTPError) and isinstance(
                    (_response := _exc.response), Response
                ):
                    if _response.status_code == HTTPStatus.NOT_FOUND:
                        raise ota_errors.OTAImageInvalid(
                            "failed to download", module=__name__
                        ) from _exc

                    if _response.status_code in [
                        HTTPStatus.FORBIDDEN,
                        HTTPStatus.UNAUTHORIZED,
                    ]:
                        raise ota_errors.UpdateRequestCookieInvalid(
                            module=__name__
                        ) from _exc
        except Exception as e:
            _generator.throw(e)
            raise
        finally:
            _exc = None  # resolve cycle ref
            _mapper.shutdown(wait=True)
            self._downloader_pool.release_all_instances()

    def _download_file_with_condition(
        self, entries: list[DownloadInfo], *, condition: threading.Condition
    ) -> DownloadResult:
        """Download a single OTA image metadata and client file with a condition.
        This method is supposed to be used for downloading metadata and client files.
        Just a wrapper around _download_single_file method.

        Returns:
            Retry counts, downloaded files size and traffic on wire.
        """
        _retry_count, _download_size, _traffic_on_wire = 0, 0, 0
        with condition:
            for entry in entries:
                _res = self._download_single_file(entry)
                _retry_count += _res.retry_count
                _download_size += _res.download_size
                _traffic_on_wire += _res.traffic_on_wire

            condition.notify()  # notify the metadata generator that this batch of download is finished
        return DownloadResult(_retry_count, _download_size, _traffic_on_wire)

    def _download_single_file(self, entry: DownloadInfo) -> DownloadResult:
        """Download a single file.
        This method is supposed to be used for any file download.
        This is the single task being executed in the downloader pool.

        Returns:
            Retry counts, downloaded files size and traffic on wire.
        """
        if (_digest := entry.digest) == EMPTY_FILE_SHA256:
            return DownloadResult(0, 0, 0)

        downloader = self._downloader_mapper[threading.get_native_id()]
        # NOTE: currently download only use sha256
        return downloader.download(
            url=entry.url,
            dst=entry.dst,
            digest=_digest,
            size=entry.original_size,
            compression_alg=entry.compression_alg,
        )

    def _download_resources(self, resource_meta: ResourceMeta) -> None:
        self._download_watchdog_ctx["previous_active_timestamp"] = int(time.time())
        _mapper = ThreadPoolExecutorWithRetry(
            max_concurrent=cfg.MAX_CONCURRENT_DOWNLOAD_TASKS,
            max_workers=cfg.DOWNLOAD_THREADS,
            thread_name_prefix="download_ota_files",
            initializer=self._downloader_worker_initializer,
            watchdog_func=partial(
                self._downloader_pool.downloading_watchdog,
                ctx=DownloadPoolWatchdogFuncContext(
                    downloaded_bytes=0,
                    previous_active_timestamp=int(time.time()),
                ),
                max_idle_timeout=cfg.DOWNLOAD_INACTIVE_TIMEOUT,
            ),
        )
        try:
            _next_commit_before, _report_batch_cnt = 0, 0
            _merged_payload = UpdateProgressReport(
                operation=UpdateProgressReport.Type.DOWNLOAD_REMOTE_COPY
            )
            for _done_count, _fut in enumerate(
                _mapper.ensure_tasks(
                    self._download_single_file,
                    resource_meta.iter_resources(
                        batch_size=cfg.MAX_CONCURRENT_DOWNLOAD_TASKS
                    ),
                ),
                start=1,
            ):
                _now = time.time()

                if _download_exception_handler(_fut):
                    err_count, file_size, downloaded_bytes = _fut.result()

                    _merged_payload.processed_file_num += 1
                    _merged_payload.processed_file_size += file_size
                    _merged_payload.errors += err_count
                    _merged_payload.downloaded_bytes += downloaded_bytes
                else:
                    _merged_payload.errors += 1

                self._metrics.downloaded_bytes = _merged_payload.downloaded_bytes
                self._metrics.downloaded_errors = _merged_payload.errors

                if (
                    _this_batch := _done_count // DOWNLOAD_STATS_REPORT_BATCH
                ) > _report_batch_cnt or _now > _next_commit_before:
                    _next_commit_before = _now + DOWNLOAD_REPORT_INTERVAL
                    _report_batch_cnt = _this_batch

                    self._status_report_queue.put_nowait(
                        StatusReport(
                            payload=_merged_payload,
                            session_id=self.session_id,
                        )
                    )

                    _merged_payload = UpdateProgressReport(
                        operation=UpdateProgressReport.Type.DOWNLOAD_REMOTE_COPY
                    )

            # for left-over items that cannot fill up the batch
            self._status_report_queue.put_nowait(
                StatusReport(
                    payload=_merged_payload,
                    session_id=self.session_id,
                )
            )
        finally:
            _mapper.shutdown(wait=True)
            # release the downloader instances
            self._downloader_pool.release_all_instances()
            self._downloader_pool.shutdown()

    def _downloader_worker_initializer(self) -> None:
        self._downloader_mapper[threading.get_native_id()] = (
            self._downloader_pool.get_instance()
        )


class _OTAUpdater(_OTAUpdateOperator):
    """The implementation of OTA update logic."""

    def __init__(
        self,
        boot_controller: BootControllerProtocol,
        **kwargs,
    ) -> None:
        # ------ init base class ------ #
        super().__init__(**kwargs)

        # ------ init updater implementation ------ #
        self._boot_controller = boot_controller

        # ------ define runtime dirs ------ #
        self._resource_dir_on_standby = Path(
            replace_root(
                cfg.OTA_TMP_STORE,
                cfg.CANONICAL_ROOT,
                self._boot_controller.get_standby_slot_path(),
            )
        )
        self._ota_tmp_meta_on_standby = Path(
            replace_root(
                cfg.OTA_TMP_META_STORE,
                cfg.CANONICAL_ROOT,
                self._boot_controller.get_standby_slot_path(),
            )
        )

        self._image_meta_dir_on_active = Path(cfg.IMAGE_META_DPATH)
        self._image_meta_dir_on_standby = Path(
            replace_root(
                cfg.IMAGE_META_DPATH,
                cfg.CANONICAL_ROOT,
                cfg.STANDBY_SLOT_MNT,
            )
        )
        self._can_use_in_place_mode = False

    def _execute_update(self):
        """Implementation of OTA updating."""
        logger.info(f"execute local update({ecu_info.ecu_id=}): {self.update_version=}")

        self._handle_upper_proxy()
        self._process_metadata()
        self._pre_update()
        self._calculate_delta()
        self._download_delta_resources()
        self._apply_update()
        self._post_update()
        self._finalize_update()

    def _pre_update(self):
        """Prepare the standby slot and optimize the file_table."""
        logger.info("enter local OTA update...")
        with TemporaryDirectory() as _tmp_dir:
            self._can_use_in_place_mode = use_inplace_mode = can_use_in_place_mode(
                dev=self._boot_controller.standby_slot_dev,
                mnt_point=_tmp_dir,
                threshold_in_bytes=int(
                    self._ota_metadata.total_regulars_size
                    * STANDBY_SLOT_USED_SIZE_THRESHOLD
                ),
            )
        logger.info(
            f"check if we can use in-place mode to update standby slot: {use_inplace_mode}"
        )

        self._boot_controller.pre_update(
            # NOTE: this option is deprecated and not used by bootcontroller
            # TODO:(20250613) when standby_as_ref is set, skip mounting active slot.
            #       we cannot do this for now, as some boot controller impl still refer to
            #       active_slot mounts.
            standby_as_ref=use_inplace_mode,
            erase_standby=not use_inplace_mode,
        )
        # prepare the tmp storage on standby slot after boot_controller.pre_update finished
        self._resource_dir_on_standby.mkdir(exist_ok=True, parents=True)
        self._ota_tmp_meta_on_standby.mkdir(exist_ok=True, parents=True)

        # NOTE(20250529): first save it to /.ota-meta, and then save it to the actual
        #                 destination folder.
        logger.info("save the OTA image file_table to standby slot ...")
        try:
            save_fstable(self._ota_metadata._fst_db, self._ota_tmp_meta_on_standby)
        except Exception as e:
            logger.error(
                f"failed to save OTA image file_table to {self._ota_tmp_meta_on_standby=}: {e!r}"
            )

    def _calculate_delta(self):
        """Calculate the delta bundle."""
        logger.info("start to calculate delta ...")
        _current_time = int(time.time())
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.CALCULATING_DELTA,
                    trigger_timestamp=_current_time,
                ),
                session_id=self.session_id,
            )
        )
        self._metrics.delta_calculation_start_timestamp = _current_time

        # NOTE(20250529): first save it to /.ota-meta, and then save it to the actual
        #                 destination folder.
        logger.info("save the OTA image file_table to standby slot ...")
        try:
            save_fstable(self._ota_metadata._fst_db, self._ota_tmp_meta_on_standby)
        except Exception as e:
            logger.error(
                f"failed to save OTA image file_table to {self._ota_tmp_meta_on_standby=}: {e!r}"
            )

        # ------ in-update: calculate delta ------ #
        logger.info("start to calculate delta ...")
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.CALCULATING_DELTA,
                    trigger_timestamp=int(time.time()),
                ),
                session_id=self.session_id,
            )
        )

        if self._can_use_in_place_mode and self._resource_dir_on_standby.is_dir():
            logger.info(
                "OTA resource dir found on standby slot, possible an interrupted OTA. \n"
                "Try to resume previous OTA delta calculation progress ..."
            )
            ResourceScanner(
                ota_metadata=self._ota_metadata,
                resource_dir=self._resource_dir_on_standby,
                status_report_queue=self._status_report_queue,
                session_id=self.session_id,
            ).resume_ota()
            logger.info("finish resuming previous OTA progress")

        base_meta_dir_on_standby_slot = None
        try:
            if self._can_use_in_place_mode:
                # try to use base file_table from standby slot itself
                base_meta_dir_on_standby_slot = (
                    self._ota_tmp_meta_on_standby / BASE_METADATA_FOLDER
                )
                shutil.rmtree(base_meta_dir_on_standby_slot, ignore_errors=True)

                # NOTE: the file_table file in /opt/ota/image-meta MUST be prepared by otaclient,
                #       it is not included in the OTA image, thus also not in file_table.
                verified_base_db = None
                if self._image_meta_dir_on_standby.is_dir():
                    shutil.move(
                        self._image_meta_dir_on_standby,
                        base_meta_dir_on_standby_slot,
                    )
                    verified_base_db = find_saved_fstable(base_meta_dir_on_standby_slot)

                _inplace_mode_params = DeltaGenParams(
                    ota_metadata=self._ota_metadata,
                    delta_src=Path(cfg.STANDBY_SLOT_MNT),
                    copy_dst=self._resource_dir_on_standby,
                    status_report_queue=self._status_report_queue,
                    session_id=self.session_id,
                )

                if verified_base_db:
                    logger.info("use in-place mode with base file table assist ...")
                    InPlaceDeltaWithBaseFileTable(**_inplace_mode_params).process_slot(
                        str(verified_base_db)
                    )
                else:
                    logger.info("use in-place mode with full scanning ...")
                    InPlaceDeltaGenFullDiskScan(**_inplace_mode_params).process_slot()
            else:
                _rebuild_mode_params = DeltaGenParams(
                    ota_metadata=self._ota_metadata,
                    delta_src=Path(cfg.ACTIVE_SLOT_MNT),
                    copy_dst=self._resource_dir_on_standby,
                    status_report_queue=self._status_report_queue,
                    session_id=self.session_id,
                )

                verified_base_db = find_saved_fstable(self._image_meta_dir_on_active)
                if verified_base_db:
                    logger.info("use rebuild mode with base file table assist ...")
                    RebuildDeltaWithBaseFileTable(**_rebuild_mode_params).process_slot(
                        str(verified_base_db)
                    )
                else:
                    logger.info("use rebuild mode with full scanning ...")
                    RebuildDeltaGenFullDiskScan(**_rebuild_mode_params).process_slot()
        except Exception as e:
            _err_msg = f"failed to generate delta: {e!r}"
            logger.exception(_err_msg)
            raise ota_errors.UpdateDeltaGenerationFailed(
                _err_msg, module=__name__
            ) from e
        finally:
            # we don't need the copy of base file table after delta calculation
            if base_meta_dir_on_standby_slot and base_meta_dir_on_standby_slot.is_dir():
                shutil.rmtree(base_meta_dir_on_standby_slot, ignore_errors=True)

    def _download_delta_resources(self) -> None:
        """Download all the resources needed for the OTA update."""
        # ------ in-update: download resources ------ #
        _current_time = int(time.time())
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.DOWNLOADING_OTA_FILES,
                    trigger_timestamp=_current_time,
                ),
                session_id=self.session_id,
            )
        )
        self._metrics.download_start_timestamp = _current_time

        # NOTE(20240705): download_files raises OTA Error directly, no need to capture exc here
        resource_meta = ResourceMeta(
            base_url=self.url_base,
            ota_metadata=self._ota_metadata,
            copy_dst=self._resource_dir_on_standby,
        )
        logger.info(
            f"delta calculation finished: \n"
            f"download_list len: {resource_meta.resources_count} \n"
            f"sum of original size of all resources to be downloaded: {human_readable_size(resource_meta.resources_size_sum)}"
        )
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=SetUpdateMetaReport(
                    total_download_files_num=resource_meta.resources_count,
                    total_download_files_size=resource_meta.resources_size_sum,
                ),
                session_id=self.session_id,
            )
        )
        self._metrics.delta_download_files_num = resource_meta.resources_count
        self._metrics.delta_download_files_size = resource_meta.resources_size_sum

        logger.info("start to download resources ...")
        try:
            self._download_resources(resource_meta)
        except TasksEnsureFailed:
            _err_msg = (
                "download aborted due to download stalls longer than "
                f"{cfg.DOWNLOAD_INACTIVE_TIMEOUT}, or otaclient process is terminated, abort OTA"
            )
            logger.error(_err_msg)
            raise ota_errors.NetworkError(_err_msg, module=__name__) from None
        finally:
            # NOTE: after this point, we don't need downloader anymore
            self._downloader_pool.shutdown()

    def _apply_update(self) -> None:
        """Apply the OTA update to the standby slot."""
        logger.info("start to apply changes to standby slot...")
        _current_time = int(time.time())
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.APPLYING_UPDATE,
                    trigger_timestamp=_current_time,
                ),
                session_id=self.session_id,
            )
        )
        self._metrics.apply_update_start_timestamp = _current_time

        try:
            standby_slot_creator = UpdateStandbySlot(
                ota_metadata=self._ota_metadata,
                standby_slot_mount_point=cfg.STANDBY_SLOT_MNT,
                status_report_queue=self._status_report_queue,
                session_id=self.session_id,
                resource_dir=self._resource_dir_on_standby,
            )
            standby_slot_creator.update_slot()
        except Exception as e:
            raise ota_errors.ApplyOTAUpdateFailed(
                f"failed to apply update to standby slot: {e!r}", module=__name__
            ) from e

    def _post_update(self) -> None:
        """Post-update phase."""
        logger.info("enter post update phase...")
        _current_time = int(time.time())
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.PROCESSING_POSTUPDATE,
                    trigger_timestamp=_current_time,
                ),
                session_id=self.session_id,
            )
        )
        self._metrics.post_update_start_timestamp = _current_time

        # NOTE(20240219): move persist file handling here
        self._process_persistents(self._ota_metadata)

        # save the OTA metadata to the actual location after
        #   standby slot rootfs updated
        ota_metadata_save_dst = Path(
            replace_root(
                cfg.IMAGE_META_DPATH,
                cfg.CANONICAL_ROOT,
                self._boot_controller.get_standby_slot_path(),
            )
        )
        ota_metadata_save_dst.mkdir(exist_ok=True, parents=True)
        shutil.rmtree(ota_metadata_save_dst, ignore_errors=True)
        shutil.move(self._ota_tmp_meta_on_standby, ota_metadata_save_dst)

        self._preserve_client_squashfs()
        self._boot_controller.post_update(self.update_version)

    def _process_persistents(self, ota_metadata: OTAMetadata):
        logger.info("start persist files handling...")
        standby_slot_mp = Path(cfg.STANDBY_SLOT_MNT)

        _handler = PersistFilesHandler(
            src_passwd_file=Path(cfg.PASSWD_FPATH),
            src_group_file=Path(cfg.GROUP_FPATH),
            dst_passwd_file=Path(standby_slot_mp / "etc/passwd"),
            dst_group_file=Path(standby_slot_mp / "etc/group"),
            src_root=cfg.ACTIVE_SLOT_MNT,
            dst_root=cfg.STANDBY_SLOT_MNT,
        )

        for persiste_entry in ota_metadata.iter_persist_entries():
            # NOTE(20240520): with update_swapfile ansible role being used wildly,
            #   now we just ignore the swapfile entries in the persistents.txt if any,
            #   and issue a warning about it.
            if persiste_entry in ["/swapfile", "/swap.img"]:
                logger.warning(
                    f"swapfile entry {persiste_entry} is listed in persistents.txt, ignored"
                )
                logger.warning(
                    (
                        "using persis file feature to preserve swapfile is MISUSE of persist file handling feature!"
                        "please change your OTA image build setting and remove swapfile entries from persistents.txt!"
                    )
                )
                continue

            try:
                _handler.preserve_persist_entry(persiste_entry)
            except Exception as e:
                _err_msg = f"failed to preserve {persiste_entry}: {e!r}, skip"
                logger.warning(_err_msg)

    def _preserve_client_squashfs(self) -> None:
        """Copy the client squashfs file to the standby slot."""
        if not _env.is_dynamic_client_running():
            logger.info(
                "dynamic client is not running, no need to copy client squashfs file"
            )
            return

        _src = Path(cfg.ACTIVE_SLOT_MNT) / Path(
            cfg.DYNAMIC_CLIENT_SQUASHFS_FILE
        ).relative_to("/")
        _dst = Path(cfg.STANDBY_SLOT_MNT) / Path(
            cfg.OTACLIENT_INSTALLATION_RELEASE
        ).relative_to("/")
        logger.info(f"copy client squashfs file from {_src} to {_dst}...")
        try:
            os.makedirs(_dst, exist_ok=True)
            shutil.copy(_src, _dst, follow_symlinks=False)
        except FileNotFoundError as e:
            logger.warning(f"failed to copy client squashfs file: {e!r}")

    def _finalize_update(self) -> None:
        """Finalize the OTA update."""
        logger.info("local update finished, wait on all subecs...")
        _current_time = int(time.time())
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.FINALIZING_UPDATE,
                    trigger_timestamp=_current_time,
                ),
                session_id=self.session_id,
            )
        )
        self._metrics.finalizing_update_start_timestamp = _current_time
        self._metrics.publish()

        if proxy_info.enable_local_ota_proxy:
            wait_and_log(
                check_flag=self.ecu_status_flags.any_child_ecu_in_update.is_set,
                check_for=False,
                message="permit reboot flag",
                log_func=logger.info,
            )

        logger.info(f"device will reboot in {WAIT_BEFORE_REBOOT} seconds!")
        time.sleep(WAIT_BEFORE_REBOOT)
        self._boot_controller.finalizing_update(
            chroot=_env.get_dynamic_client_chroot_path()
        )

    # API

    def execute(self) -> None:
        """Main entry for executing local OTA update.

        Handles OTA failure and logging/finalizing on failure.
        """
        try:
            self._execute_update()
            shutil.rmtree(self._resource_dir_on_standby, ignore_errors=True)
        except ota_errors.OTAError as e:
            logger.error(f"update failed: {e!r}")
            self._boot_controller.on_operation_failure()
            raise  # do not cover the OTA error again
        except Exception as e:
            _err_msg = f"unspecific error, update failed: {e!r}"
            self._boot_controller.on_operation_failure()
            raise ota_errors.ApplyOTAUpdateFailed(_err_msg, module=__name__) from e
        finally:
            self._metrics.publish()
            ensure_umount(self._session_workdir, ignore_error=True)
            shutil.rmtree(self._session_workdir, ignore_errors=True)


class _OTAClientUpdater(_OTAUpdateOperator):
    """The implementation of OTA client update logic."""

    def __init__(
        self,
        client_update_control_flags: ClientUpdateControlFlags,
        **kwargs,
    ) -> None:
        # ------ init base class ------ #
        super().__init__(**kwargs)

        # --- Event flag to control client update ---- #
        self.client_update_control_flags = client_update_control_flags

        # ------ setup OTA client package parser ------ #
        self._ota_client_package = OTAClientPackageDownloader(
            base_url=self.url_base,
            ota_metadata=self._ota_metadata,
            session_dir=self._session_workdir,
            package_install_dir=cfg.OTACLIENT_INSTALLATION_RELEASE,
            squashfs_file=cfg.DYNAMIC_CLIENT_SQUASHFS_FILE,
        )

    def _execute_client_update(self):
        """Implementation of OTA updating."""
        logger.info(f"execute local update({ecu_info.ecu_id=}): {self.update_version=}")

        try:
            self._handle_upper_proxy()
            self._process_metadata()
            self._download_client_package_resources()
            self._wait_sub_ecus()
            if self._is_same_client_package_version():
                # to notify the status report after reboot
                self._request_shutdown()
            else:
                self._copy_client_package()
                self._notify_data_ready()
        except Exception as e:
            logger.warning(f"failed to run squashfs: {e!r}")
            raise

    def _download_client_package_resources(self) -> None:
        """Download OTA client."""
        # ------ in-update: download resources ------ #
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.DOWNLOADING_OTA_CLIENT,
                    trigger_timestamp=int(time.time()),
                ),
                session_id=self.session_id,
            )
        )

        try:
            logger.info("start to download client manifest and package...")
            self._download_client_package_files()
        except TasksEnsureFailed:
            _err_msg = (
                "download aborted due to download stalls longer than "
                f"{cfg.DOWNLOAD_INACTIVE_TIMEOUT}, or otaclient process is terminated, abort OTA"
            )
            logger.error(_err_msg)
            raise ota_errors.NetworkError(_err_msg, module=__name__) from None
        finally:
            # NOTE: after this point, we don't need downloader anymore
            self._downloader_pool.shutdown()

    def _download_client_package_files(self) -> None:
        self._download_and_process_file_with_condition(
            thread_name_prefix="download_client_file",
            get_downloads_generator=self._ota_client_package.download_client_package,
        )

    def _wait_sub_ecus(self) -> None:
        logger.info("wait for all sub-ECU to finish...")
        # wait for all sub-ECU to finish OTA update
        result = wait_and_log(
            check_flag=self.ecu_status_flags.any_child_ecu_in_update.is_set,
            check_for=False,
            message="client updating in sub ecus",
            log_func=logger.info,
            timeout=cfg.CLIENT_UPDATE_TIMEOUT,
        )
        if result is False:
            logger.warning("sub-ECU client was aborted, skip waiting for sub-ecus")

    def _is_same_client_package_version(self) -> bool:
        return self._ota_client_package.is_same_client_package_version()

    def _copy_client_package(self) -> None:
        self._ota_client_package.copy_client_package()

    def _notify_data_ready(self):
        """Notify the main process that the client package is ready."""
        logger.info("notify main process that the client package is ready..")
        self.client_update_control_flags.notify_data_ready_event.set()

    def _request_shutdown(self):
        """Request shutdown."""
        # TODO(airkei) [2025-06-19]: should return the dedicated error code for "client update"
        self.client_update_control_flags.request_shutdown_event.set()

    # API

    def execute(self) -> None:
        """Main entry for executing local OTA client update."""
        try:
            self._execute_client_update()
        except Exception as e:
            logger.error(f"client update failed: {e!r}")
            raise
        finally:
            shutil.rmtree(self._session_workdir, ignore_errors=True)


class _OTARollbacker:
    def __init__(self, boot_controller: BootControllerProtocol) -> None:
        self._boot_controller = boot_controller

    def execute(self):
        try:
            self._boot_controller.pre_rollback()
            self._boot_controller.post_rollback()
            self._boot_controller.finalizing_rollback()
        except ota_errors.OTAError as e:
            logger.error(f"rollback failed: {e!r}")
            self._boot_controller.on_operation_failure()
            raise


class OTAClient:
    def __init__(
        self,
        *,
        ecu_status_flags: MultipleECUStatusFlags,
        proxy: Optional[str] = None,
        status_report_queue: Queue[StatusReport],
        client_update_control_flags: ClientUpdateControlFlags,
    ) -> None:
        self.my_ecu_id = ecu_info.ecu_id
        self.proxy = proxy
        self.ecu_status_flags = ecu_status_flags

        self._status_report_queue = status_report_queue
        self._client_update_control_flags = client_update_control_flags
        self._live_ota_status = OTAStatus.INITIALIZED
        self.started = False

        self._runtime_dir = _runtime_dir = Path(cfg.RUN_DIR)
        _runtime_dir.mkdir(exist_ok=True, parents=True)
        self._update_session_dir = _update_session_dir = Path(cfg.RUNTIME_OTA_SESSION)

        # NOTE: for each otaclient instance lifecycle, only one tmpfs will be mounted.
        #       If otaclient terminates by signal, umounting will be handled by _on_shutdown.
        #       If otaclient exits on successful OTA, no need to umount it manually as we will reboot soon.
        ensure_umount(_update_session_dir, ignore_error=True)
        _update_session_dir.mkdir(exist_ok=True, parents=True)
        try:
            ensure_mount(
                "tmpfs",
                _update_session_dir,
                mount_func=partial(
                    mount_tmpfs, size_in_mb=cfg.SESSION_WD_TMPFS_SIZE_IN_MB
                ),
                raise_exception=True,
            )
        except Exception as e:
            logger.warning(f"failed to mount tmpfs for OTA runtime use: {e!r}")
            logger.warning("will directly use /run tmpfs for OTA runtime!")

        self._metrics = OTAMetricsData()
        self._metrics.ecu_id = self.my_ecu_id

        try:
            _boot_controller_type = get_boot_controller(ecu_info.bootloader)
        except Exception as e:
            self._on_failure(
                e,
                ota_status=OTAStatus.FAILURE,
                failure_type=FailureType.UNRECOVERABLE,
                failure_reason=f"failed to determine boot controller or create_standby mode: {e!r}",
            )
            return

        try:
            self.boot_controller = _boot_controller_type()
        except Exception as e:
            self._on_failure(
                e,
                ota_status=OTAStatus.FAILURE,
                failure_type=FailureType.UNRECOVERABLE,
                failure_reason=f"boot controller startup failed: {e!r}",
            )
            return

        # load and report booted OTA status
        _boot_ctrl_loaded_ota_status = self.boot_controller.get_booted_ota_status()
        self._live_ota_status = _boot_ctrl_loaded_ota_status
        self.current_version = self.boot_controller.load_version()

        status_report_queue.put_nowait(
            StatusReport(
                payload=SetOTAClientMetaReport(
                    firmware_version=self.current_version,
                ),
            )
        )
        status_report_queue.put_nowait(
            StatusReport(
                payload=OTAStatusChangeReport(
                    new_ota_status=_boot_ctrl_loaded_ota_status,
                ),
            )
        )
        self._metrics.current_firmware_version = self.current_version

        self.ca_chains_store = None
        try:
            self.ca_chains_store = load_ca_cert_chains(cfg.CERT_DPATH)
        except CACertStoreInvalid as e:
            _err_msg = f"failed to import ca_chains_store: {e!r}, OTA will NOT occur on no CA chains installed!!!"
            logger.error(_err_msg)

            self.ca_chains_store = CAChainStore()

        self.started = True
        logger.info("otaclient started")

    def _on_failure(
        self,
        exc: Exception,
        *,
        ota_status: OTAStatus,
        failure_reason: str,
        failure_type: FailureType,
    ) -> None:
        try:
            _traceback = get_traceback(exc)

            logger.error(failure_reason)
            logger.error(f"last error traceback: \n{_traceback}")

            self._status_report_queue.put_nowait(
                StatusReport(
                    payload=OTAStatusChangeReport(
                        new_ota_status=ota_status,
                        failure_type=failure_type,
                        failure_reason=failure_reason,
                        failure_traceback=_traceback,
                    ),
                )
            )
            self._metrics.failure_type = failure_type
            self._metrics.failure_reason = failure_reason
            self._metrics.failed_at_phase = ota_status
        finally:
            del exc  # prevent ref cycle

    def _exit_from_dynamic_client(self) -> None:
        """Exit from dynamic client."""
        if not _env.is_dynamic_client_running():
            # dynamic client is not running, no need to exit
            return

        logger.info("exit from dynamic client...")
        self._client_update_control_flags.request_shutdown_event.set()

    # API

    @property
    def live_ota_status(self) -> OTAStatus:
        return self._live_ota_status

    @property
    def is_busy(self) -> bool:
        return self._live_ota_status in [
            OTAStatus.UPDATING,
            OTAStatus.ROLLBACKING,
            OTAStatus.CLIENT_UPDATING,
        ]

    def update(self, request: UpdateRequestV2) -> None:
        """
        NOTE that update API will not raise any exceptions. The failure information
            is available via status API.
        """
        self._live_ota_status = OTAStatus.UPDATING
        new_session_id = request.session_id
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAStatusChangeReport(
                    new_ota_status=OTAStatus.UPDATING,
                ),
                session_id=new_session_id,
            )
        )
        logger.info(f"start new OTA update session: {new_session_id=}")

        session_wd = self._update_session_dir / new_session_id
        try:
            logger.info("[update] entering local update...")
            if not self.ca_chains_store:
                raise ota_errors.MetadataJWTVerficationFailed(
                    "no CA chains are installed, reject any OTA update",
                    module=__name__,
                )

            _OTAUpdater(
                version=request.version,
                raw_url_base=request.url_base,
                cookies_json=request.cookies_json,
                session_wd=session_wd,
                ca_chains_store=self.ca_chains_store,
                boot_controller=self.boot_controller,
                ecu_status_flags=self.ecu_status_flags,
                upper_otaproxy=self.proxy,
                status_report_queue=self._status_report_queue,
                session_id=new_session_id,
                metrics=replace(self._metrics, session_id=new_session_id),
            ).execute()
        except ota_errors.OTAError as e:
            self._live_ota_status = OTAStatus.FAILURE
            self._on_failure(
                e,
                ota_status=OTAStatus.FAILURE,
                failure_reason=e.get_failure_reason(),
                failure_type=e.failure_type,
            )
            self._exit_from_dynamic_client()
        finally:
            shutil.rmtree(session_wd, ignore_errors=True)

    def client_update(self, request: ClientUpdateRequestV2) -> None:
        """
        NOTE that client update API will not raise any exceptions. The failure information
            is available via status API.
        """
        if _env.is_dynamic_client_running():
            # Duplicates client update should not be allowed.
            # TODO(airkei) [2025-06-19]: should return the dedicated error code for "client update"
            return

        self._live_ota_status = OTAStatus.CLIENT_UPDATING
        new_session_id = request.session_id
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAStatusChangeReport(
                    new_ota_status=OTAStatus.CLIENT_UPDATING,
                ),
                session_id=new_session_id,
            )
        )
        logger.info(f"start new OTA client update session: {new_session_id=}")

        session_wd = self._update_session_dir / new_session_id
        try:
            logger.info("[client update] entering local update...")
            if not self.ca_chains_store:
                raise ota_errors.MetadataJWTVerficationFailed(
                    "no CA chains are installed, reject any OTA update",
                    module=__name__,
                )

            _OTAClientUpdater(
                version=request.version,
                raw_url_base=request.url_base,
                cookies_json=request.cookies_json,
                session_wd=session_wd,
                ca_chains_store=self.ca_chains_store,
                ecu_status_flags=self.ecu_status_flags,
                upper_otaproxy=self.proxy,
                status_report_queue=self._status_report_queue,
                session_id=new_session_id,
                client_update_control_flags=self._client_update_control_flags,
                metrics=self._metrics,
            ).execute()
        except ota_errors.OTAError:
            # TODO(airkei) [2025-06-19]: should return the dedicated error code for "client update"
            self._client_update_control_flags.request_shutdown_event.set()
        except Exception:
            self._client_update_control_flags.request_shutdown_event.set()

    def rollback(self, request: RollbackRequestV2) -> None:
        self._live_ota_status = OTAStatus.ROLLBACKING
        new_session_id = request.session_id
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAStatusChangeReport(
                    new_ota_status=OTAStatus.ROLLBACKING,
                ),
                session_id=new_session_id,
            )
        )

        logger.info(f"start new OTA rollback session: {new_session_id=}")
        try:
            logger.info("[rollback] entering...")
            self._live_ota_status = OTAStatus.ROLLBACKING
            _OTARollbacker(boot_controller=self.boot_controller).execute()
        except ota_errors.OTAError as e:
            self._on_failure(
                e,
                ota_status=OTAStatus.FAILURE,
                failure_reason=e.get_failure_reason(),
                failure_type=e.failure_type,
            )

    def main(
        self,
        *,
        req_queue: mp_queue.Queue[IPCRequest],
        resp_queue: mp_queue.Queue[IPCResponse],
    ) -> NoReturn:
        """Main loop of ota_core process."""
        _allow_request_after = 0
        while True:
            _now = int(time.time())
            try:
                request = req_queue.get(timeout=OP_CHECK_INTERVAL)
            except Empty:
                continue

            if _now < _allow_request_after or self.is_busy:
                _err_msg = (
                    f"otaclient is busy at {self._live_ota_status} or "
                    f"request too quickly({_allow_request_after=}), "
                    f"reject {request}"
                )
                logger.warning(_err_msg)
                resp_queue.put_nowait(
                    IPCResponse(
                        res=IPCResEnum.REJECT_BUSY,
                        msg=_err_msg,
                        session_id=request.session_id,
                    )
                )

            elif isinstance(request, UpdateRequestV2):
                _update_thread = threading.Thread(
                    target=self.update,
                    args=[request],
                    daemon=True,
                    name="ota_update_executor",
                )
                _update_thread.start()

                resp_queue.put_nowait(
                    IPCResponse(
                        res=IPCResEnum.ACCEPT,
                        session_id=request.session_id,
                    )
                )
                _allow_request_after = _now + HOLD_REQ_HANDLING_ON_ACK_REQUEST

            elif isinstance(request, ClientUpdateRequestV2):
                _client_update_thread = threading.Thread(
                    target=self.client_update,
                    args=[request],
                    daemon=True,
                    name="ota_client_update_executor",
                )
                _client_update_thread.start()

                resp_queue.put_nowait(
                    IPCResponse(
                        res=IPCResEnum.ACCEPT,
                        session_id=request.session_id,
                    )
                )
                _allow_request_after = _now + HOLD_REQ_HANDLING_ON_ACK_REQUEST

            elif (
                isinstance(request, RollbackRequestV2)
                and self._live_ota_status == OTAStatus.SUCCESS
            ):
                _rollback_thread = threading.Thread(
                    target=self.rollback,
                    args=[request],
                    daemon=True,
                    name="ota_rollback_executor",
                )
                _rollback_thread.start()

                resp_queue.put_nowait(
                    IPCResponse(
                        res=IPCResEnum.ACCEPT,
                        session_id=request.session_id,
                    )
                )
                _allow_request_after = _now + HOLD_REQ_HANDLING_ON_ACK_REQUEST
            else:
                _err_msg = f"request is invalid: {request=}, {self._live_ota_status=}"
                logger.error(_err_msg)
                resp_queue.put_nowait(
                    IPCResponse(
                        res=IPCResEnum.REJECT_OTHER,
                        msg=_err_msg,
                        session_id=request.session_id,
                    )
                )


def ota_core_process(
    *,
    shm_writer_factory: Callable[[], SharedOTAClientStatusWriter],
    ecu_status_flags: MultipleECUStatusFlags,
    op_queue: mp_queue.Queue[IPCRequest],
    resp_queue: mp_queue.Queue[IPCResponse],
    max_traceback_size: int,  # in bytes
    client_update_control_flags: ClientUpdateControlFlags,
):
    from otaclient._logging import configure_logging
    from otaclient.configs.cfg import proxy_info
    from otaclient.ota_core import OTAClient

    configure_logging()

    shm_writer = shm_writer_factory()

    _local_status_report_queue = Queue()
    _status_monitor = OTAClientStatusCollector(
        msg_queue=_local_status_report_queue,
        shm_status=shm_writer,
        max_traceback_size=max_traceback_size,
    )
    _status_monitor.start()
    _status_monitor.start_log_thread()

    _ota_core = OTAClient(
        ecu_status_flags=ecu_status_flags,
        proxy=proxy_info.get_proxy_for_local_ota(),
        status_report_queue=_local_status_report_queue,
        client_update_control_flags=client_update_control_flags,
    )
    _ota_core.main(req_queue=op_queue, resp_queue=resp_queue)
