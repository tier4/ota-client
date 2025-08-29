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
from urllib.parse import urlparse

from ota_metadata.legacy2 import _errors as ota_metadata_error
from ota_metadata.legacy2.metadata import OTAMetadata
from ota_metadata.utils.cert_store import CAChainStore
from otaclient import errors as ota_errors
from otaclient._status_monitor import (
    OTAUpdatePhaseChangeReport,
    SetUpdateMetaReport,
    StatusReport,
)
from otaclient._types import CriticalZoneFlags, MultipleECUStatusFlags, UpdatePhase
from otaclient._utils import SharedOTAClientMetricsReader
from otaclient.configs.cfg import cfg
from otaclient.metrics import OTAMetricsData
from otaclient_common.common import ensure_otaproxy_start
from otaclient_common.downloader import DownloaderPool

from ._common import download_exception_handler
from ._download_resources import DownloadHelper

logger = logging.getLogger(__name__)

WAIT_FOR_OTAPROXY_ONLINE = 3 * 60  # 3mins


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
        critical_zone_flags: CriticalZoneFlags,
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

        # ------ init critical zone flags ------ #
        self.critical_zone_flags = critical_zone_flags

        # ------ init variables needed for update ------ #
        _url_base = urlparse(raw_url_base)
        _path = f"{_url_base.path.rstrip('/')}/"
        self.url_base = _url_base._replace(path=_path).geturl()

        # ------ setup downloader ------ #
        self._downloader_pool = _downloader_pool = DownloaderPool(
            instance_num=cfg.DOWNLOAD_THREADS,
            hash_func=sha256,
            chunk_size=cfg.CHUNK_SIZE,
            cookies=cookies,
            # NOTE(20221013): check requests document for how to set proxy,
            #                 we only support using http proxy here.
            proxies={"http": upper_otaproxy} if upper_otaproxy else None,
        )
        self._download_helper = DownloadHelper(
            downloader_pool=_downloader_pool,
            max_concurrent=cfg.MAX_CONCURRENT_DOWNLOAD_TASKS,
            download_inactive_timeout=cfg.DOWNLOAD_INACTIVE_TIMEOUT,
        )

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
        try:
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
