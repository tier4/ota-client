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
import os
import threading
import time
from concurrent.futures import Future
from functools import partial
from hashlib import sha256
from http import HTTPStatus
from json.decoder import JSONDecodeError
from pathlib import Path
from queue import Queue
from typing import Any, Iterator, Optional, Type
from urllib.parse import urlparse

import requests.exceptions as requests_exc
from requests import Response

from ota_metadata.legacy import parser as ota_metadata_parser
from ota_metadata.legacy import types as ota_metadata_types
from ota_metadata.utils.cert_store import (
    CACertStoreInvalid,
    CAChainStore,
    load_ca_cert_chains,
)
from otaclient import errors as ota_errors
from otaclient._status_monitor import (
    OTAStatusChangeReport,
    OTAUpdatePhaseChangeReport,
    SetOTAClientMetaReport,
    SetUpdateMetaReport,
    StatusReport,
    UpdateProgressReport,
)
from otaclient._types import FailureType, OTAStatus, UpdatePhase, UpdateRequestV2
from otaclient.boot_control import BootControllerProtocol, get_boot_controller
from otaclient.configs.cfg import cfg, ecu_info
from otaclient.create_standby import (
    StandbySlotCreatorProtocol,
    get_standby_slot_creator,
)
from otaclient.create_standby.common import DeltaBundle
from otaclient.utils import get_traceback, wait_and_log
from otaclient_common.common import ensure_otaproxy_start
from otaclient_common.downloader import (
    EMPTY_FILE_SHA256,
    Downloader,
    DownloaderPool,
    DownloadPoolWatchdogFuncContext,
)
from otaclient_common.persist_file_handling import PersistFilesHandler
from otaclient_common.retry_task_map import ThreadPoolExecutorWithRetry

logger = logging.getLogger(__name__)

DEFAULT_STATUS_QUERY_INTERVAL = 1
WAIT_BEFORE_REBOOT = 6
DOWNLOAD_STATS_REPORT_BATCH = 300
DOWNLOAD_REPORT_INTERVAL = 1  # second


class OTAClientError(Exception): ...


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


class _OTAUpdater:
    """The implementation of OTA update logic."""

    def __init__(
        self,
        *,
        version: str,
        raw_url_base: str,
        cookies_json: str,
        ca_chains_store: CAChainStore,
        upper_otaproxy: str | None = None,
        boot_controller: BootControllerProtocol,
        create_standby_cls: Type[StandbySlotCreatorProtocol],
        control_flags: OTAClientControlFlags,
        status_report_queue: Queue[StatusReport],
        session_id: str,
    ) -> None:
        self.ca_chains_store = ca_chains_store
        self.session_id = session_id
        self._status_report_queue = status_report_queue

        # ------ define OTA temp paths ------ #
        self._ota_tmp_on_standby = Path(cfg.STANDBY_SLOT_MNT) / Path(
            cfg.OTA_TMP_STORE
        ).relative_to("/")
        self._ota_tmp_image_meta_dir_on_standby = Path(cfg.STANDBY_SLOT_MNT) / Path(
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
                probing_timeout=cfg.DOWNLOAD_INACTIVE_TIMEOUT,
            )
            # NOTE(20221013): check requests document for how to set proxy,
            #                 we only support using http proxy here.
            proxies["http"] = upper_otaproxy

        # ------ init updater implementation ------ #
        self._control_flags = control_flags
        self._boot_controller = boot_controller
        self._create_standby_cls = create_standby_cls

        # ------ init update status ------ #
        self.update_version = version
        self.update_start_timestamp = int(time.time())

        status_report_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.INITIALIZING,
                    trigger_timestamp=self.update_start_timestamp,
                ),
                session_id=self.session_id,
            )
        )
        status_report_queue.put_nowait(
            StatusReport(
                payload=SetUpdateMetaReport(
                    update_firmware_version=version,
                ),
                session_id=self.session_id,
            )
        )

        # ------ init variables needed for update ------ #
        _url_base = urlparse(raw_url_base)
        _path = f"{_url_base.path.rstrip('/')}/"
        self.url_base = _url_base._replace(path=_path).geturl()

        # ------ setup downloader ------ #
        self._downloader_pool = DownloaderPool(
            instance_num=cfg.DOWNLOAD_THREADS,
            hash_func=sha256,
            chunk_size=cfg.CHUNK_SIZE,
            cookies=cookies,
            proxies=proxies,
        )
        self._downloader_mapper: dict[int, Downloader] = {}

    def _calculate_delta(
        self,
        standby_slot_creator: StandbySlotCreatorProtocol,
    ) -> DeltaBundle:
        logger.info("start to calculate and prepare delta...")
        delta_bundle = standby_slot_creator.calculate_and_prepare_delta()

        # update dynamic information
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=SetUpdateMetaReport(
                    total_download_files_num=len(delta_bundle.download_list),
                    total_download_files_size=delta_bundle.total_download_files_size,
                    total_remove_files_num=len(delta_bundle.rm_delta),
                ),
                session_id=self.session_id,
            )
        )
        return delta_bundle

    def _download_files(
        self,
        ota_metadata: ota_metadata_parser.OTAMetadata,
        download_list: Iterator[ota_metadata_types.RegularInf],
    ):
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

        def _download_file(
            entry: ota_metadata_types.RegularInf,
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

            entry_url, compression_alg = ota_metadata.get_download_url(entry)
            downloader = self._downloader_mapper[threading.get_native_id()]
            return downloader.download(
                entry_url,
                self._ota_tmp_on_standby / _fhash_str,
                digest=_fhash_str,
                size=entry.size,
                compression_alg=compression_alg,
            )

        with ThreadPoolExecutorWithRetry(
            max_concurrent=cfg.MAX_CONCURRENT_DOWNLOAD_TASKS,
            max_workers=cfg.DOWNLOAD_THREADS,
            thread_name_prefix="download_ota_files",
            initializer=_thread_initializer,
            watchdog_func=partial(
                self._downloader_pool.downloading_watchdog,
                ctx=DownloadPoolWatchdogFuncContext(
                    downloaded_bytes=0,
                    previous_active_timestamp=int(time.time()),
                ),
                max_idle_timeout=cfg.DOWNLOAD_INACTIVE_TIMEOUT,
            ),
        ) as _mapper:
            _next_commit_before, _report_batch_cnt = 0, 0
            _merged_payload = UpdateProgressReport(
                operation=UpdateProgressReport.Type.DOWNLOAD_REMOTE_COPY
            )

            for _done_count, _fut in enumerate(
                _mapper.ensure_tasks(_download_file, download_list), start=1
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

        # release the downloader instances
        self._downloader_pool.release_all_instances()
        self._downloader_pool.shutdown()

    def _process_persistents(self, ota_metadata: ota_metadata_parser.OTAMetadata):
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

        for _perinf in ota_metadata.iter_metafile(
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

            try:
                _handler.preserve_persist_entry(_per_fpath)
            except Exception as e:
                _err_msg = f"failed to preserve {_per_fpath}: {e!r}, skip"
                logger.warning(_err_msg)

    def _execute_update(self):
        """Implementation of OTA updating."""
        logger.info(f"execute local update({ecu_info.ecu_id=}): {self.update_version=}")

        # ------ init, processing metadata ------ #
        logger.debug("process metadata.jwt...")
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.PROCESSING_METADATA,
                    trigger_timestamp=int(time.time()),
                ),
                session_id=self.session_id,
            )
        )

        try:
            # TODO(20240619): ota_metadata should not be responsible for downloading anything
            otameta = ota_metadata_parser.OTAMetadata(
                url_base=self.url_base,
                downloader=self._downloader_pool.get_instance(),
                run_dir=Path(cfg.RUN_DIR),
                ca_chains_store=self.ca_chains_store,
            )
            self._status_report_queue.put_nowait(
                StatusReport(
                    payload=SetUpdateMetaReport(
                        image_file_entries=otameta.total_files_num,
                        image_size_uncompressed=otameta.total_files_size_uncompressed,
                        metadata_downloaded_bytes=self._downloader_pool.total_downloaded_bytes,
                    ),
                    session_id=self.session_id,
                )
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
        except ota_metadata_parser.OTAImageInvalid as e:
            _err_msg = f"OTA image is invalid: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.OTAImageInvalid(_err_msg, module=__name__) from e
        except ota_metadata_parser.OTARequestsAuthTokenInvalid as e:
            _err_msg = f"OTA requests auth token is invalid: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.UpdateRequestCookieInvalid(
                _err_msg, module=__name__
            ) from e
        except Exception as e:
            _err_msg = f"failed to prepare ota metafiles: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.OTAMetaDownloadFailed(_err_msg, module=__name__) from e
        finally:
            self._downloader_pool.release_instance()

        # ------ pre-update ------ #
        logger.info("enter local OTA update...")
        self._boot_controller.pre_update(
            self.update_version,
            standby_as_ref=False,  # NOTE: this option is deprecated and not used by bootcontroller
            erase_standby=self._create_standby_cls.should_erase_standby_slot(),
        )
        # prepare the tmp storage on standby slot after boot_controller.pre_update finished
        self._ota_tmp_on_standby.mkdir(exist_ok=True)
        self._ota_tmp_image_meta_dir_on_standby.mkdir(exist_ok=True)

        # ------ in-update ------ #
        standby_slot_creator = self._create_standby_cls(
            ota_metadata=otameta,
            boot_dir=str(self._boot_controller.get_standby_boot_dir()),
            active_slot_mount_point=cfg.ACTIVE_SLOT_MNT,
            standby_slot_mount_point=cfg.STANDBY_SLOT_MNT,
            status_report_queue=self._status_report_queue,
            session_id=self.session_id,
        )
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.CALCULATING_DELTA,
                    trigger_timestamp=int(time.time()),
                ),
                session_id=self.session_id,
            )
        )

        try:
            delta_bundle = self._calculate_delta(standby_slot_creator)
        except Exception as e:
            _err_msg = f"failed to generate delta: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.UpdateDeltaGenerationFailed(
                _err_msg, module=__name__
            ) from e

        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.DOWNLOADING_OTA_FILES,
                    trigger_timestamp=int(time.time()),
                ),
                session_id=self.session_id,
            )
        )
        # NOTE(20240705): download_files raises OTA Error directly, no need to capture exc here
        try:
            self._download_files(otameta, delta_bundle.get_download_list())
        finally:
            del delta_bundle
            self._downloader_pool.shutdown()

        # ------ apply update ------ #
        logger.info("start to apply changes to standby slot...")
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.APPLYING_UPDATE,
                    trigger_timestamp=int(time.time()),
                ),
                session_id=self.session_id,
            )
        )
        standby_slot_creator.create_standby_slot()

        # ------ post-update ------ #
        logger.info("enter post update phase...")
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.PROCESSING_POSTUPDATE,
                    trigger_timestamp=int(time.time()),
                ),
                session_id=self.session_id,
            )
        )
        # NOTE(20240219): move persist file handling here
        self._process_persistents(otameta)
        self._boot_controller.post_update()

        # ------ finalizing update ------ #
        logger.info("local update finished, wait on all subecs...")
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.FINALIZING_UPDATE,
                    trigger_timestamp=int(time.time()),
                ),
                session_id=self.session_id,
            )
        )
        wait_and_log(
            flag=self._control_flags._can_reboot,
            message="permit reboot flag",
            log_func=logger.info,
        )

        logger.info(f"device will reboot in {WAIT_BEFORE_REBOOT} seconds!")
        time.sleep(WAIT_BEFORE_REBOOT)
        self._boot_controller.finalizing_update()

    # API

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
    """
    Init params:
        boot_controller: boot control instance
        create_standby_cls: type of create standby slot mechanism to use
        my_ecu_id: ECU id of the device running this otaclient instance
        control_flags: flags used by otaclient and ota_service stub for synchronization
        proxy: upper otaproxy URL
    """

    def __init__(
        self,
        *,
        control_flags: OTAClientControlFlags,
        proxy: Optional[str] = None,
        status_report_queue: Queue[StatusReport],
    ) -> None:
        self.my_ecu_id = ecu_info.ecu_id
        self.proxy = proxy
        self.control_flags = control_flags

        self._status_report_queue = status_report_queue
        self._live_ota_status = OTAStatus.INITIALIZED
        self.started = False

        try:
            _boot_controller_type = get_boot_controller(ecu_info.bootloader)
            self.create_standby_cls = get_standby_slot_creator(
                cfg.CREATE_STANDBY_METHOD
            )
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
        status_report_queue.put_nowait(
            StatusReport(
                payload=OTAStatusChangeReport(
                    new_ota_status=_boot_ctrl_loaded_ota_status,
                ),
            )
        )

        self.current_version = self.boot_controller.load_version()
        status_report_queue.put_nowait(
            StatusReport(
                payload=SetOTAClientMetaReport(
                    firmware_version=self.current_version,
                ),
            )
        )

        self.ca_chains_store = None
        try:
            self.ca_chains_store = load_ca_cert_chains(cfg.CERT_DPATH)
        except CACertStoreInvalid as e:
            _err_msg = f"failed to import ca_chains_store: {e!r}, OTA will NOT occur on no CA chains installed!!!"
            logger.error(_err_msg)

            self.ca_chains_store = CAChainStore()

        self.started = True
        logger.info("otaclient started")

    def _gen_session_id(self, update_version: str = "") -> str:
        """Generate a unique session_id for the new OTA session.

        token schema:
            <update_version>-<unix_timestamp_in_sec_str>-<4bytes_hex>
        """
        _time_factor = str(int(time.time()))
        _random_factor = os.urandom(4).hex()

        return f"{update_version}-{_time_factor}-{_random_factor}"

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
        finally:
            del exc  # prevent ref cycle

    # API

    @property
    def live_ota_status(self) -> OTAStatus:
        return self._live_ota_status

    @property
    def is_busy(self) -> bool:
        return self._live_ota_status in [OTAStatus.UPDATING, OTAStatus.ROLLBACKING]

    def update(self, request: UpdateRequestV2) -> None:
        """
        NOTE that update API will not raise any exceptions. The failure information
            is available via status API.
        """
        if self.is_busy:
            return

        new_session_id = self._gen_session_id(request.version)
        self._status_report_queue.put_nowait(
            StatusReport(
                payload=OTAStatusChangeReport(
                    new_ota_status=OTAStatus.UPDATING,
                ),
                session_id=new_session_id,
            )
        )
        logger.info(f"start new OTA update session: {new_session_id=}")

        try:
            logger.info("[update] entering local update...")
            if not self.ca_chains_store:
                raise ota_errors.MetadataJWTVerficationFailed(
                    "no CA chains are installed, reject any OTA update",
                    module=__name__,
                )

            self._live_ota_status = OTAStatus.UPDATING
            _OTAUpdater(
                version=request.version,
                raw_url_base=request.url_base,
                cookies_json=request.cookies_json,
                ca_chains_store=self.ca_chains_store,
                boot_controller=self.boot_controller,
                create_standby_cls=self.create_standby_cls,
                control_flags=self.control_flags,
                upper_otaproxy=self.proxy,
                status_report_queue=self._status_report_queue,
                session_id=new_session_id,
            ).execute()
        except ota_errors.OTAError as e:
            self._live_ota_status = OTAStatus.FAILURE
            self._on_failure(
                e,
                ota_status=OTAStatus.FAILURE,
                failure_reason=e.get_failure_reason(),
                failure_type=e.failure_type,
            )

    def rollback(self) -> None:
        if self.is_busy:
            return

        new_session_id = self._gen_session_id("___rollback")
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
            self._live_ota_status = OTAStatus.FAILURE
            self._on_failure(
                e,
                ota_status=OTAStatus.FAILURE,
                failure_reason=e.get_failure_reason(),
                failure_type=e.failure_type,
            )
