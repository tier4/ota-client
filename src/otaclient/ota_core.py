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
"""Implementation of OTA logic, composing with boot control and standby slot update."""


from __future__ import annotations

import errno
import gc
import itertools
import json
import logging
import multiprocessing.synchronize as mp_sync
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

from ota_metadata.legacy import parser as ota_metadata_parser
from ota_metadata.legacy import types as ota_metadata_types
from otaclient import __version__
from otaclient._types import FailureType
from otaclient.boot_control import BootControllerProtocol, get_boot_controller
from otaclient.create_standby import (
    StandbySlotCreatorProtocol,
    get_standby_slot_creator,
)
from otaclient.create_standby.common import DeltaBundle
from otaclient.stats_monitor import (
    OTAStatus,
    OTAStatusChangeReport,
    OTAUpdatePhaseChangeReport,
    SetOTAClientMetaReport,
    SetUpdateMetaReport,
    StatsReport,
    StatsReportType,
    UpdatePhase,
    UpdateProgressReport,
)
from otaclient_common.common import ensure_otaproxy_start
from otaclient_common.downloader import (
    EMPTY_FILE_SHA256,
    Downloader,
    DownloaderPool,
    DownloadPoolWatchdogFuncContext,
)
from otaclient_common.persist_file_handling import PersistFilesHandler
from otaclient_common.retry_task_map import ThreadPoolExecutorWithRetry

from . import errors as ota_errors
from .app.configs import config as cfg
from .app.configs import ecu_info

logger = logging.getLogger(__name__)

DEFAULT_STATUS_QUERY_INTERVAL = 1


class LiveOTAStatus:
    def __init__(self, ota_status: OTAStatus) -> None:
        self.live_ota_status = ota_status

    def get_ota_status(self) -> OTAStatus:
        return self.live_ota_status

    def set_ota_status(self, _status: OTAStatus):
        self.live_ota_status = _status

    def request_update(self) -> bool:
        return self.live_ota_status in [
            OTAStatus.INITIALIZED,
            OTAStatus.SUCCESS,
            OTAStatus.FAILURE,
            OTAStatus.ROLLBACK_FAILURE,
        ]

    def request_rollback(self) -> bool:
        return self.live_ota_status in [
            OTAStatus.SUCCESS,
            OTAStatus.ROLLBACK_FAILURE,
        ]


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
        del exc, _fut  # drop ref to exc instance


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
        control_flags: mp_sync.Event,
        stats_report_queue: Queue[StatsReport],
        session_id: str,
    ) -> None:
        self._stats_report_queue = stats_report_queue

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
        self.update_version = version
        self.update_start_timestamp = int(time.time())

        self.session_id = session_id
        stats_report_queue.put_nowait(
            StatsReport(
                type=StatsReportType.SET_OTA_UPDATE_PHASE,
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.INITIALIZING,
                    trigger_timestamp=self.update_start_timestamp,
                ),
                session_id=self.session_id,
            )
        )
        stats_report_queue.put_nowait(
            StatsReport(
                type=StatsReportType.SET_OTA_UPDATE_META,
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

        # ------ information from OTA image meta and delta generation ------ #
        self.total_files_size_uncompressed = 0
        self.total_files_num = 0
        self.total_download_files_num = 0
        self.total_download_fiies_size = 0
        self.total_remove_files_num = 0

        # ------ setup downloader ------ #
        self._downloader_pool = DownloaderPool(
            instance_num=cfg.MAX_DOWNLOAD_THREAD,
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

        self._stats_report_queue.put_nowait(
            StatsReport(
                type=StatsReportType.SET_OTA_UPDATE_META,
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
            max_workers=cfg.MAX_DOWNLOAD_THREAD,
            thread_name_prefix="download_ota_files",
            initializer=_thread_initializer,
            watchdog_func=partial(
                self._downloader_pool.downloading_watchdog,
                ctx=DownloadPoolWatchdogFuncContext(
                    downloaded_bytes=0,
                    previous_active_timestamp=int(time.time()),
                ),
                max_idle_timeout=cfg.DOWNLOAD_GROUP_INACTIVE_TIMEOUT,
            ),
        ) as _mapper:
            for _fut in _mapper.ensure_tasks(_download_file, download_list):
                if _download_exception_handler(_fut):  # donwload succeeded
                    err_count, file_size, downloaded_bytes = _fut.result()

                    self._stats_report_queue.put_nowait(
                        StatsReport(
                            type=StatsReportType.SET_OTA_UPDATE_PROGRESS,
                            payload=UpdateProgressReport(
                                operation=UpdateProgressReport.Type.DOWNLOAD_REMOTE_COPY,
                                processed_file_num=1,
                                processed_file_size=file_size,
                                errors=err_count,
                                downloaded_bytes=downloaded_bytes,
                            ),
                            session_id=self.session_id,
                        )
                    )
                else:  # download failed, but exceptions can be handled
                    self._stats_report_queue.put_nowait(
                        StatsReport(
                            type=StatsReportType.SET_OTA_UPDATE_PROGRESS,
                            payload=UpdateProgressReport(
                                operation=UpdateProgressReport.Type.DOWNLOAD_REMOTE_COPY,
                                errors=1,
                            ),
                            session_id=self.session_id,
                        )
                    )

        # release the downloader instances
        self._downloader_pool.release_all_instances()
        self._downloader_pool.shutdown()

    def _process_persistents(self, ota_metadata: ota_metadata_parser.OTAMetadata):
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
        logger.info(f"execute local update: {self.update_version=},{self.url_base=}")

        # ------ init, processing metadata ------ #
        logger.debug("process metadata.jwt...")
        self._stats_report_queue.put_nowait(
            StatsReport(
                type=StatsReportType.SET_OTA_UPDATE_PHASE,
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
                certs_dir=Path(cfg.CERTS_DIR),
            )
            self.total_files_num = otameta.total_files_num
            self.total_files_size_uncompressed = otameta.total_files_size_uncompressed
            self._stats_report_queue.put_nowait(
                StatsReport(
                    type=StatsReportType.SET_OTA_UPDATE_META,
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
            standby_slot_mount_point=cfg.MOUNT_POINT,
            active_slot_mount_point=cfg.ACTIVE_ROOT_MOUNT_POINT,
            stats_report_queue=self._stats_report_queue,
            session_id=self.session_id,
        )

        self._stats_report_queue.put_nowait(
            StatsReport(
                type=StatsReportType.SET_OTA_UPDATE_PHASE,
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

        self._stats_report_queue.put_nowait(
            StatsReport(
                type=StatsReportType.SET_OTA_UPDATE_PHASE,
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

        self._stats_report_queue.put_nowait(
            StatsReport(
                type=StatsReportType.SET_OTA_UPDATE_PHASE,
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.APPLYING_UPDATE,
                    trigger_timestamp=int(time.time()),
                ),
                session_id=self.session_id,
            )
        )
        logger.info("start to apply changes to standby slot...")
        standby_slot_creator.create_standby_slot()

        # ------ post-update ------ #
        logger.info("enter post update phase...")
        self._stats_report_queue.put_nowait(
            StatsReport(
                type=StatsReportType.SET_OTA_UPDATE_PHASE,
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.PROCESSING_POSTUPDATE,
                    trigger_timestamp=int(time.time()),
                ),
                session_id=self.session_id,
            )
        )
        # NOTE(20240219): move persist file handling here
        self._process_persistents(otameta)

        # boot controller postupdate
        next(_postupdate_gen := self._boot_controller.post_update())

        logger.info("local update finished, wait on all subecs...")
        self._stats_report_queue.put_nowait(
            StatsReport(
                type=StatsReportType.SET_OTA_UPDATE_PHASE,
                payload=OTAUpdatePhaseChangeReport(
                    new_update_phase=UpdatePhase.FINALIZING_UPDATE,
                    trigger_timestamp=int(time.time()),
                ),
                session_id=self.session_id,
            )
        )

        logger.info("wait for permit reboot flag set ...")
        for seconds in itertools.count(start=1):
            if self._control_flags.is_set():
                break

            if seconds // 30 == 0:
                logger.info(f"wait for reboot flag set: {seconds}s passed ...")
            time.sleep(1)

        logger.info("device will reboot now")
        next(_postupdate_gen, None)  # reboot

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
    def __init__(
        self,
        boot_controller: BootControllerProtocol,
        *,
        stats_report_queue: Queue[StatsReport],
    ) -> None:
        self._boot_controller = boot_controller
        self._stats_report_queue = stats_report_queue

    def execute(self):
        try:
            self._boot_controller.pre_rollback()
            self._boot_controller.post_rollback()
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
        control_flag: flags used by otaclient and ota_service stub for synchronization
        proxy: upper otaproxy URL
    """

    OTACLIENT_VERSION = __version__

    def __init__(
        self,
        *,
        control_flag: mp_sync.Event,
        proxy: Optional[str] = None,
        stats_report_queue: Queue[StatsReport],
    ):
        self.my_ecu_id = ecu_info.ecu_id
        self._stats_report_queue = stats_report_queue

        self.create_standby_cls = get_standby_slot_creator(cfg.STANDBY_CREATION_MODE)
        try:
            self.boot_controller = get_boot_controller(ecu_info.bootloader)()
        except ota_errors.OTAError as e:
            logger.error(
                e.get_error_report(title=f"boot controller startup failed: {e!r}")
            )

            stats_report_queue.put_nowait(
                StatsReport(
                    type=StatsReportType.SET_OTA_STATUS,
                    payload=OTAStatusChangeReport(
                        new_ota_status=OTAStatus.FAILURE,
                        failure_type=FailureType.UNRECOVERABLE,
                        failure_reason=e.get_failure_reason(),
                    ),
                )
            )
            return

        try:
            # load and report booted OTA status
            _boot_ctrl_loaded_ota_status = self.boot_controller.get_booted_ota_status()
            stats_report_queue.put_nowait(
                StatsReport(
                    type=StatsReportType.SET_OTA_STATUS,
                    payload=OTAStatusChangeReport(
                        new_ota_status=_boot_ctrl_loaded_ota_status,
                    ),
                )
            )
            self._live_ota_status = LiveOTAStatus(
                self.boot_controller.get_booted_ota_status()
            )

            # load and report current running system image version
            self.current_version = self.boot_controller.load_version()
            stats_report_queue.put_nowait(
                StatsReport(
                    type=StatsReportType.SET_OTACLIENT_META,
                    payload=SetOTAClientMetaReport(
                        firmware_version=self.current_version,
                    ),
                )
            )

            self.proxy = proxy
            self.control_flags = control_flag
        except Exception as e:
            _err_msg = f"failed to start otaclient core: {e!r}"
            logger.error(_err_msg)
            raise ota_errors.OTAClientStartupFailed(_err_msg, module=__name__) from e

    def _on_failure(
        self,
        exc: ota_errors.OTAError,
        ota_status: OTAStatus,
        *,
        session_id: str,
    ):
        self._live_ota_status.set_ota_status(ota_status)
        try:
            self.last_failure_type = exc.failure_type
            self.last_failure_reason = exc.get_failure_reason()
            if cfg.DEBUG_MODE:
                self.last_failure_traceback = exc.get_failure_traceback()

            self._stats_report_queue.put_nowait(
                StatsReport(
                    type=StatsReportType.SET_OTA_STATUS,
                    payload=OTAStatusChangeReport(
                        new_ota_status=(
                            OTAStatus.FAILURE
                            if ota_status == OTAStatus.FAILURE
                            else OTAStatus.ROLLBACK_FAILURE
                        ),
                    ),
                    session_id=session_id,
                )
            )

            logger.error(
                exc.get_error_report(f"OTA failed with {ota_status.name}: {exc!r}")
            )
        finally:
            del exc  # prevent ref cycle

    def _gen_session_id(self, update_version: str = "") -> str:
        """Generate a unique session_id for the new OTA session.

        token schema:
            <update_version>-<unix_timestamp_in_sec_str>-<4bytes_hex>
        """
        _time_factor = str(int(time.time()))
        _random_factor = os.urandom(4).hex()

        return f"{update_version}-{_time_factor}-{_random_factor}"

    # API

    @property
    def live_ota_status(self) -> LiveOTAStatus:
        """Exposed for checking whether current OTAClient should start new OTA session or not."""
        return self._live_ota_status

    def update(self, version: str, url_base: str, cookies_json: str) -> None:
        self._live_ota_status.set_ota_status(OTAStatus.UPDATING)
        new_session_id = self._gen_session_id(version)
        self._stats_report_queue.put_nowait(
            StatsReport(
                type=StatsReportType.SET_OTA_STATUS,
                payload=OTAStatusChangeReport(
                    new_ota_status=OTAStatus.UPDATING,
                ),
                session_id=new_session_id,
            )
        )

        try:
            logger.info("[update] entering local update...")
            _OTAUpdater(
                version=version,
                raw_url_base=url_base,
                cookies_json=cookies_json,
                boot_controller=self.boot_controller,
                create_standby_cls=self.create_standby_cls,
                control_flags=self.control_flags,
                upper_otaproxy=self.proxy,
                stats_report_queue=self._stats_report_queue,
                session_id=new_session_id,
            ).execute()
        except ota_errors.OTAError as e:
            self._on_failure(e, OTAStatus.FAILURE, session_id=new_session_id)
        finally:
            gc.collect()  # trigger a forced gc

    def rollback(self):
        self._live_ota_status.set_ota_status(OTAStatus.ROLLBACKING)
        new_session_id = self._gen_session_id("<rollback>")
        self._stats_report_queue.put_nowait(
            StatsReport(
                type=StatsReportType.SET_OTA_STATUS,
                payload=OTAStatusChangeReport(
                    new_ota_status=OTAStatus.ROLLBACKING,
                ),
                session_id=new_session_id,
            )
        )

        try:
            logger.info("[rollback] entering...")
            _rollback_executor = _OTARollbacker(
                boot_controller=self.boot_controller,
                stats_report_queue=self._stats_report_queue,
            )

            # entering rollback
            _rollback_executor.execute()
        # silently ignore overlapping request
        except ota_errors.OTAError as e:
            self._on_failure(e, OTAStatus.ROLLBACK_FAILURE, session_id=new_session_id)
        finally:
            _rollback_executor = None