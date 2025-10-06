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

import logging
import os
import shutil
import threading
import time
from abc import abstractmethod
from pathlib import Path
from queue import Queue
from typing import TypedDict
from urllib.parse import urlparse

from ota_image_libs._crypto.x509_utils import CACertStore
from ota_image_libs.v1.image_index.schema import ImageIdentifier
from ota_image_libs.v1.image_manifest.schema import OTAReleaseKey
from typing_extensions import Unpack

from ota_metadata.legacy2.metadata import OTAMetadata
from ota_metadata.utils.cert_store import CAChainStore
from ota_metadata.v1 import OTAImageHelper
from otaclient import errors as ota_errors
from otaclient._status_monitor import (
    OTAUpdatePhaseChangeReport,
    SetUpdateMetaReport,
    StatusReport,
)
from otaclient._types import CriticalZoneFlag, MultipleECUStatusFlags, UpdatePhase
from otaclient._utils import SharedOTAClientMetricsReader
from otaclient.boot_control.protocol import BootControllerProtocol
from otaclient.configs.cfg import cfg, ecu_info
from otaclient.create_standby._common import ResourcesDigestWithSize
from otaclient.metrics import OTAMetricsData
from otaclient.ota_core._common import download_exception_handler
from otaclient.ota_core._download_resources import (
    DownloadHelperForLegacyOTAImage,
    DownloadHelperForOTAImageV1,
)
from otaclient.ota_core._update_libs import metadata_download_err_handler
from otaclient_common import _env, replace_root
from otaclient_common.cmdhelper import ensure_umount
from otaclient_common.downloader import DownloaderPool

logger = logging.getLogger(__name__)


class OTAProtocol:
    critical_zone_flag: CriticalZoneFlag
    _boot_controller: BootControllerProtocol
    update_version: str
    _session_workdir: Path

    @abstractmethod
    def _process_metadata(self): ...

    @abstractmethod
    def _pre_update(self): ...

    @abstractmethod
    def _in_update(self): ...

    @abstractmethod
    def _download_delta_resources(
        self, delta_digests: ResourcesDigestWithSize
    ) -> None: ...

    @abstractmethod
    def _post_update(self): ...

    @abstractmethod
    def _finalize_update(self): ...

    def execute(self) -> None:
        """Main entry for executing local OTA update.

        Handles OTA failure and logging/finalizing on failure.
        """
        logger.info(f"execute local update({ecu_info.ecu_id=}): {self.update_version=}")
        try:
            self._process_metadata()
            with self.critical_zone_flag.acquire_lock_with_release() as _lock_acquired:
                if not _lock_acquired:
                    logger.error(
                        "Unable to acquire critical zone lock during pre-update phase, as OTA is already stopping"
                    )
                    raise ota_errors.OTAStopRequested(module=__name__)

                logger.info("Entering critical zone for OTA update: pre-update phase")

                self._pre_update()

            self._in_update()

            with self.critical_zone_flag.acquire_lock_with_release() as _lock_acquired:
                if not _lock_acquired:
                    logger.error(
                        "Unable to acquire critical zone lock during post-update and finalize-update phases, as OTA is already stopping"
                    )
                    raise ota_errors.OTAStopRequested(module=__name__)

                logger.info(
                    "Entering critical zone for OTA update: post-update and finalize-update phases"
                )
                self._post_update()
                self._finalize_update()

            # NOTE(20250818): not delete the OTA resource dir to speed up next OTA
        except ota_errors.OTAError as e:
            logger.error(f"update failed: {e!r}")
            self._boot_controller.on_operation_failure()
            raise  # do not cover the OTA error again
        except Exception as e:
            _err_msg = f"unspecific error, update failed: {e!r}"
            self._boot_controller.on_operation_failure()
            raise ota_errors.ApplyOTAUpdateFailed(_err_msg, module=__name__) from e
        finally:
            ensure_umount(self._session_workdir, ignore_error=True)
            shutil.rmtree(self._session_workdir, ignore_errors=True)


class OTAUpdateOperatorInit(TypedDict):
    version: str
    raw_url_base: str
    session_wd: Path
    downloader_pool: DownloaderPool
    ecu_status_flags: MultipleECUStatusFlags
    status_report_queue: Queue[StatusReport]
    session_id: str
    metrics: OTAMetricsData
    shm_metrics_reader: SharedOTAClientMetricsReader


class OTAUpdateOperator:
    """The base common class of OTA update logic."""

    def __init__(
        self,
        *,
        version: str,
        raw_url_base: str,
        session_wd: Path,
        downloader_pool: DownloaderPool,
        ecu_status_flags: MultipleECUStatusFlags,
        status_report_queue: Queue[StatusReport],
        session_id: str,
        metrics: OTAMetricsData,
        shm_metrics_reader: SharedOTAClientMetricsReader,
    ) -> None:
        self.update_version = version
        self.update_start_timestamp = int(time.time())
        self.session_id = session_id
        self._status_report_queue = status_report_queue
        self._metrics = metrics
        self._shm_metrics_reader = shm_metrics_reader

        # ------ define runtime dirs ------ #
        self._active_slot_mp = Path(cfg.ACTIVE_SLOT_MNT)
        self._resource_dir_on_active = Path(
            replace_root(
                cfg.OTA_RESOURCES_STORE,
                cfg.CANONICAL_ROOT,
                self._active_slot_mp,
            )
        )

        self._standby_slot_mp = Path(cfg.STANDBY_SLOT_MNT)
        self._resource_dir_on_standby = Path(
            replace_root(
                cfg.OTA_RESOURCES_STORE,
                cfg.CANONICAL_ROOT,
                self._standby_slot_mp,
            )
        )
        self._ota_meta_store_on_standby = Path(
            replace_root(
                cfg.OTA_META_STORE,
                cfg.CANONICAL_ROOT,
                self._standby_slot_mp,
            )
        )
        self._ota_meta_store_base_on_standby = Path(
            replace_root(
                cfg.OTA_META_STORE_BASE_FILE_TABLE,
                cfg.CANONICAL_ROOT,
                self._standby_slot_mp,
            )
        )
        self._image_meta_dir_on_standby = Path(
            replace_root(
                cfg.IMAGE_META_DPATH,
                cfg.CANONICAL_ROOT,
                self._standby_slot_mp,
            )
        )

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
        self._downloader_pool = downloader_pool

    def _preserve_ota_image_meta_at_post_update(self):
        self._ota_meta_store_on_standby.mkdir(exist_ok=True, parents=True)
        # after update_slot finished, we can finally remove the previous base file_table.
        shutil.rmtree(self._ota_meta_store_base_on_standby, ignore_errors=True)

        # save the filetable to /opt/ota/image-meta
        shutil.rmtree(self._image_meta_dir_on_standby, ignore_errors=True)
        self._image_meta_dir_on_standby.mkdir(exist_ok=True, parents=True)
        shutil.copytree(
            self._ota_meta_store_on_standby,
            self._image_meta_dir_on_standby,
            dirs_exist_ok=True,
        )

        # prepare base file_table to the base OTA meta store for next OTA
        self._ota_meta_store_base_on_standby.mkdir(exist_ok=True, parents=True)
        for entry in self._ota_meta_store_on_standby.iterdir():
            if entry.is_file():
                shutil.move(str(entry), self._ota_meta_store_base_on_standby)

    def _preserve_client_squashfs_at_post_update(self) -> None:
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


class OTAUpdateOperatorInitLegacy(OTAUpdateOperatorInit):
    ca_chains_store: CAChainStore


class OTAUpdateOperatorLegacyBase(OTAUpdateOperator):
    def __init__(
        self,
        *,
        ca_chains_store: CAChainStore,
        **kwargs: Unpack[OTAUpdateOperatorInit],
    ):
        super().__init__(**kwargs)

        # ------ setup OTA metadata parser ------ #
        self._ota_metadata = OTAMetadata(
            base_url=self.url_base,
            session_dir=self._session_workdir,
            ca_chains_store=ca_chains_store,
        )

        self._download_helper = DownloadHelperForLegacyOTAImage(
            downloader_pool=self._downloader_pool,
            max_concurrent=cfg.MAX_CONCURRENT_DOWNLOAD_TASKS,
            download_inactive_timeout=cfg.DOWNLOAD_INACTIVE_TIMEOUT,
        )

    def _process_metadata(self, only_metadata_verification: bool = False) -> None:
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

        logger.info("verify and download OTA image metadata ...")
        with metadata_download_err_handler():
            _condition = threading.Condition()
            for _fut in self._download_helper.download_meta_files(
                self._ota_metadata.download_metafiles(
                    _condition,
                    only_metadata_verification=only_metadata_verification,
                ),
                condition=_condition,
            ):
                download_exception_handler(_fut)

        _metadata_jwt = self._ota_metadata.metadata_jwt

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


DEFAULT_IMAGE_ID = ImageIdentifier(
    ecu_id=ecu_info.ecu_id,
    release_key=OTAReleaseKey.dev,
)


class OTAUpdateOperatorInitOTAImageV1(OTAUpdateOperatorInit):
    ca_store: CACertStore


class OTAUpdateOperatorOTAImageV1Base(OTAUpdateOperator):
    def __init__(
        self,
        *,
        ca_store: CACertStore,
        image_identifier: ImageIdentifier = DEFAULT_IMAGE_ID,
        **kwargs: Unpack[OTAUpdateOperatorInit],
    ) -> None:
        super().__init__(**kwargs)
        self._image_id = image_identifier
        self._download_helper = DownloadHelperForOTAImageV1(
            downloader_pool=self._downloader_pool,
            max_concurrent=cfg.MAX_CONCURRENT_DOWNLOAD_TASKS,
            download_inactive_timeout=cfg.DOWNLOAD_INACTIVE_TIMEOUT,
        )
        self._ota_image_helper = OTAImageHelper(
            session_dir=self._session_workdir,
            base_url=self.url_base,
            ca_store=ca_store,
        )

        # NOTE(20250916): currently OTA download dir is only used by
        #                 new OTA image support implementation.
        self._download_tmp_on_standby = Path(
            replace_root(
                cfg.OTA_DOWNLOAD_DIR,
                cfg.CANONICAL_ROOT,
                self._standby_slot_mp,
            )
        )

    def _process_metadata(self, only_metadata_verification: bool = False):
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

        logger.info("verify and download OTA image metadata ...")
        with metadata_download_err_handler():
            _condition = threading.Condition()
            for _fut in self._download_helper.download_meta_files(
                self._ota_image_helper.download_and_verify_image_index(
                    condition=_condition,
                ),
                condition=_condition,
            ):
                download_exception_handler(_fut)

            if only_metadata_verification:
                return

            for _fut in self._download_helper.download_meta_files(
                self._ota_image_helper.select_image_payload(
                    self._image_id,
                    condition=_condition,
                ),
                condition=_condition,
            ):
                download_exception_handler(_fut)

        image_config = self._ota_image_helper.image_config
        assert image_config
        regular_files_count = image_config.sys_image_regular_files_count
        sys_image_size = image_config.sys_image_size or 0

        logger.info(
            "ota_metadata parsed finished: \n"
            f"total_regulars_num: {regular_files_count} \n"
            f"total_regulars_size: {sys_image_size}"
        )

        self._status_report_queue.put_nowait(
            StatusReport(
                payload=SetUpdateMetaReport(
                    image_file_entries=regular_files_count,
                    image_size_uncompressed=sys_image_size,
                    metadata_downloaded_bytes=self._downloader_pool.total_downloaded_bytes,
                ),
                session_id=self.session_id,
            )
        )
        self._metrics.ota_image_total_files_size = sys_image_size
        self._metrics.ota_image_total_regulars_num = regular_files_count
        self._metrics.ota_image_total_directories_num = (
            image_config.sys_image_dirs_count
        )
        self._metrics.ota_image_total_symlinks_num = (
            image_config.sys_image_non_regular_files_count
        )
