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

import json
import logging
import threading
import time
from hashlib import sha256
from json.decoder import JSONDecodeError
from pathlib import Path
from queue import Queue
from typing import TypedDict
from urllib.parse import urlparse

from typing_extensions import NotRequired, Unpack

from ota_metadata.legacy2.metadata import OTAMetadata
from ota_metadata.utils.cert_store import CAChainStore
from otaclient import errors as ota_errors
from otaclient._status_monitor import (
    OTAUpdatePhaseChangeReport,
    SetUpdateMetaReport,
    StatusReport,
)
from otaclient._types import MultipleECUStatusFlags, UpdatePhase
from otaclient._utils import SharedOTAClientMetricsReader
from otaclient.configs.cfg import cfg
from otaclient.metrics import OTAMetricsData
from otaclient.ota_core._common import download_exception_handler
from otaclient.ota_core._download_resources import DownloadHelperForLegacyOTAImage
from otaclient.ota_core._update_libs import metadata_download_err_handler
from otaclient_common import replace_root
from otaclient_common.downloader import DownloaderPool

logger = logging.getLogger(__name__)


class OTAUpdateOperatorInit(TypedDict):
    version: str
    raw_url_base: str
    cookies_json: str
    session_wd: Path
    ca_chains_store: CAChainStore
    upper_otaproxy: NotRequired[str | None]
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
        cookies_json: str,
        session_wd: Path,
        ca_chains_store: CAChainStore,
        upper_otaproxy: str | None = None,
        ecu_status_flags: MultipleECUStatusFlags,
        status_report_queue: Queue[StatusReport],
        session_id: str,
        metrics: OTAMetricsData,
        shm_metrics_reader: SharedOTAClientMetricsReader,
    ) -> None:
        self._ca_chains_store = ca_chains_store

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

        # ------ parse cookies ------ #
        logger.debug("process cookies_json...")
        try:
            cookies = json.loads(cookies_json)
            assert isinstance(cookies, dict), (
                f"invalid cookies, expecting json object: {cookies_json}"
            )
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
        self._downloader_pool = DownloaderPool(
            instance_num=cfg.DOWNLOAD_THREADS,
            hash_func=sha256,
            chunk_size=cfg.CHUNK_SIZE,
            cookies=cookies,
            # NOTE(20221013): check requests document for how to set proxy,
            #                 we only support using http proxy here.
            proxies={"http": upper_otaproxy} if upper_otaproxy else None,
        )


class OTAUpdateOperatorLegacyOTAImage(OTAUpdateOperator):
    def __init__(self, **kwargs: Unpack[OTAUpdateOperatorInit]):
        super().__init__(**kwargs)

        # ------ setup OTA metadata parser ------ #
        self._ota_metadata = OTAMetadata(
            base_url=self.url_base,
            session_dir=self._session_workdir,
            ca_chains_store=self._ca_chains_store,
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
