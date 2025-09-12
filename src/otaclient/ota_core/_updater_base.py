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
import threading
import time
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
from otaclient._status_monitor import (
    OTAUpdatePhaseChangeReport,
    SetUpdateMetaReport,
    StatusReport,
)
from otaclient._types import MultipleECUStatusFlags, UpdatePhase
from otaclient._utils import SharedOTAClientMetricsReader
from otaclient.configs.cfg import cfg, ecu_info
from otaclient.metrics import OTAMetricsData
from otaclient.ota_core._common import download_exception_handler
from otaclient.ota_core._download_resources import (
    DownloadHelperForLegacyOTAImage,
    DownloadHelperForOTAImageV1,
)
from otaclient.ota_core._update_libs import metadata_download_err_handler
from otaclient_common import replace_root
from otaclient_common.downloader import DownloaderPool

logger = logging.getLogger(__name__)


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


class OTAUpdateOperatorInitLegacy(OTAUpdateOperatorInit):
    ca_store: CAChainStore


class OTAUpdateOperatorLegacyBase(OTAUpdateOperator):
    def __init__(
        self, *, ca_store: CAChainStore, **kwargs: Unpack[OTAUpdateOperatorInit]
    ):
        super().__init__(**kwargs)

        # ------ setup OTA metadata parser ------ #
        self._ota_metadata = OTAMetadata(
            base_url=self.url_base,
            session_dir=self._session_workdir,
            ca_chains_store=ca_store,
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
